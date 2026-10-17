package ibcontroller.agent;

import java.awt.AWTEvent;
import java.awt.Dialog;
import java.awt.Frame;
import java.awt.Toolkit;
import java.awt.Window;
import java.awt.event.WindowEvent;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.channels.Channels;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * {@code AWTEventListener} install and NDJSON event forwarding over the event socket (Java
 * Agent document sec 04, sec 05, sec 07). Bridges {@code window_opened}/{@code window_closed}
 * only.
 *
 * <p>The {@code AWTEventListener} callback runs on the EDT -- it must never block on socket I/O,
 * so it only enqueues (a bounded queue; a full queue is reported via an {@code overflow}
 * message, never silently dropped); a dedicated writer thread does the actual socket write.
 */
final class EventBridge {

    private static final Logger LOG = Logger.getLogger(EventBridge.class.getName());

    private static final int QUEUE_CAPACITY = 256;
    private static final long KEEPALIVE_INTERVAL_MS = 30_000;

    private static final BlockingQueue<Map<String, Object>> queue =
            new ArrayBlockingQueue<>(QUEUE_CAPACITY);
    private static final AtomicLong seq = new AtomicLong();
    private static final AtomicBoolean overflowed = new AtomicBoolean();

    /**
     * Assigns each open window an opaque, stable ID, the analog of IBC's {@code WindowHandler}
     * callbacks receiving the {@code Window} object (its {@code SwingUtils.findButton}/
     * {@code findCheckBox}/{@code findTextField} take an explicit container). The JSON socket
     * cannot hand Python a Java object, so Python echoes the ID back on a later command and
     * {@link #resolveWindow} maps it to the window. {@code WriteOps} then scopes its component
     * search to that window instead of scanning every {@code Window.getWindows()}, which can
     * hit the wrong one of two simultaneously open "OK" buttons.
     *
     * <p>{@code Window}/{@code Component} don't override {@code equals}/{@code hashCode}, so a
     * plain identity comparison is already what a {@code Map} keyed on them gives us -- no need
     * for {@code IdentityHashMap} specifically. {@code ConcurrentHashMap} (both directions, kept
     * in sync together) because {@link #registerWindow}/{@link #unregisterWindow} run on the EDT
     * ({@code onAwtEvent}, guaranteed by the JDK) while {@link #resolveWindow} is read from the
     * command-socket thread ({@code Protocol.handleCommandClient}, via {@code WriteOps}) and
     * {@link #registeredWindows} from the event-socket thread -- three different threads, genuinely
     * concurrent, unlike the plain {@code HashMap}s the rest of this class otherwise uses for
     * single-threaded, per-message construction.
     */
    private static final Map<Window, String> windowIds = new ConcurrentHashMap<>();

    private static final Map<String, Window> windowsById = new ConcurrentHashMap<>();
    private static final AtomicLong nextWindowId = new AtomicLong(1);

    /**
     * Held while the id registry changes together with the matching queue offer, and while a new
     * client's snapshot is taken together with the queue reset, so the stream a client sees is
     * {@code snapshot}, then only events newer than it. Anything written before the snapshot is
     * older than it.
     */
    private static final Object LOCK = new Object();

    private static volatile PrintWriter currentWriter;

    private EventBridge() {}

    /**
     * Installed once, at agent startup, before the target app creates any window (a listener
     * installed later misses the first one, as in IBC). Must be called before the entry point
     * is launched; {@link AgentMain} enforces that ordering, not this method.
     */
    static void install() {
        Toolkit.getDefaultToolkit()
                .addAWTEventListener(EventBridge::onAwtEvent, AWTEvent.WINDOW_EVENT_MASK);
        startWriter();
        startDaemon(EventBridge::keepaliveLoop, "ibcontroller-event-keepalive");
    }

    private static void startDaemon(Runnable task, String name) {
        Thread thread = new Thread(task, name);
        thread.setDaemon(true);
        thread.start();
    }

    private static void onAwtEvent(AWTEvent event) {
        if (!(event instanceof WindowEvent windowEvent)) {
            return;
        }
        String kind =
                switch (windowEvent.getID()) {
                    case WindowEvent.WINDOW_OPENED -> "window_opened";
                    case WindowEvent.WINDOW_CLOSED -> "window_closed";
                    default -> null;
                };
        if (kind == null) {
            return;
        }
        Window window = windowEvent.getWindow();
        synchronized (LOCK) {
            String id =
                    "window_opened".equals(kind) ? registerWindow(window) : unregisterWindow(window);
            enqueueEvent(kind, describeWindow(window, id));
        }
    }

    /** Queues one window event; a full queue is reported via the {@code overflow} flag. */
    static void enqueueEvent(String kind, Map<String, Object> window) {
        synchronized (LOCK) {
            Map<String, Object> message = new LinkedHashMap<>();
            message.put("type", "event");
            message.put("seq", seq.incrementAndGet());
            message.put("kind", kind);
            message.put("window", window);
            if (!queue.offer(message)) {
                overflowed.set(true);
                LOG.warning("event queue full (" + QUEUE_CAPACITY + "): dropped window event "
                        + kind + " " + window.get("class"));
            }
        }
    }

    /** Called only from {@link #onAwtEvent}, which the JDK guarantees runs on the EDT. */
    private static String registerWindow(Window window) {
        String id = "w" + nextWindowId.getAndIncrement();
        windowIds.put(window, id);
        windowsById.put(id, window);
        LOG.fine("registered " + id + " -> " + window.getClass().getName() + " \""
                + String.valueOf(titleOf(window)) + "\"");
        return id;
    }

    /** Without this, a long-running instance would leak one map entry (both directions) per
     * window ever opened, for the rest of the process's life. */
    private static String unregisterWindow(Window window) {
        String id = windowIds.remove(window);
        if (id != null) {
            windowsById.remove(id);
            LOG.fine("unregistered " + id);
        }
        return id;
    }

    /** Resolves a window ID back to the live {@link Window} object -- the one place a command
     * handler ({@code WriteOps}) goes to scope a lookup to a specific window. Returns {@code
     * null} if the window has since closed (a real, expected race between Python recognising a
     * window and a later command actually reaching the agent) or the ID was never valid --
     * {@code WriteOps} turns that into a distinguishable {@code window_gone} error, not a crash.
     */
    static Window resolveWindow(String id) {
        return windowsById.get(id);
    }

    private static Map<String, Object> describeWindow(Window window, String id) {
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("class", window.getClass().getName());
        String title = titleOf(window);
        if (title != null) {
            info.put("title", title);
        }
        if (id != null) {
            info.put("window_id", id);
        }
        return info;
    }

    private static String titleOf(Window window) {
        if (window instanceof Frame frame) {
            return frame.getTitle();
        }
        if (window instanceof Dialog dialog) {
            return dialog.getTitle();
        }
        return null;
    }

    private static void writerLoop() {
        while (true) {
            Map<String, Object> message;
            try {
                message = queue.take();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            PrintWriter writer = currentWriter;
            if (writer == null) {
                continue;
            }
            if (overflowed.compareAndSet(true, false)) {
                writeMessage(writer, overflowMessage());
            }
            writeMessage(writer, message);
        }
    }

    private static Map<String, Object> overflowMessage() {
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("type", "overflow");
        message.put("from_seq", seq.get());
        return message;
    }

    private static void keepaliveLoop() {
        while (true) {
            try {
                Thread.sleep(KEEPALIVE_INTERVAL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            PrintWriter writer = currentWriter;
            if (writer != null) {
                Map<String, Object> message = new LinkedHashMap<>();
                message.put("type", "keepalive");
                message.put("ts", System.currentTimeMillis());
                writeMessage(writer, message);
            }
        }
    }

    private static void writeMessage(PrintWriter writer, Map<String, Object> message) {
        writer.println(Protocol.Json.write(message));
        writer.flush();
    }

    /**
     * Event-socket accept loop handler: {@code hello}, then {@code snapshot}, then push events
     * as they occur (Java Agent document sec 07). One client at a time, matching the command
     * socket's own posture (sec 09) -- a new connection simply replaces the current writer.
     */
    static void handleEventClient(SocketChannel client) {
        try (BufferedReader in =
                        new BufferedReader(Channels.newReader(client, StandardCharsets.UTF_8));
                PrintWriter out =
                        new PrintWriter(Channels.newWriter(client, StandardCharsets.UTF_8), true)) {
            LOG.fine("event client connected");
            attachClient(out, EventBridge::registeredWindows);
            // Push-only: the client sends nothing on this socket. Block on read purely to
            // detect disconnection (EOF) -- there's nothing to do with an actual line if one
            // somehow arrived.
            while (in.readLine() != null) {
                // ignored
            }
        } catch (IOException e) {
            LOG.log(Level.WARNING, "event client error: " + e.getMessage(), e);
        } finally {
            currentWriter = null;
            LOG.fine("event client disconnected");
        }
    }

    /**
     * Sends {@code hello}, then makes {@code out} the event stream's target with a {@code
     * snapshot} queued first. Under {@link #LOCK}: the writer is set, events queued before this
     * client (already reflected in the snapshot) are dropped, and the snapshot goes in ahead of
     * every later event. An older event the writer thread already took may still be written
     * before the snapshot; the snapshot supersedes it.
     */
    static void attachClient(PrintWriter out, Supplier<List<Object>> windows) {
        sendHello(out);
        synchronized (LOCK) {
            currentWriter = out;
            queue.clear();
            overflowed.set(false);
            Map<String, Object> snapshot = new LinkedHashMap<>();
            snapshot.put("type", "snapshot");
            snapshot.put("windows", windows.get());
            queue.offer(snapshot);
        }
    }

    private static void sendHello(PrintWriter out) {
        Map<String, Object> hello = new LinkedHashMap<>();
        hello.put("type", "hello");
        hello.put("protocol_version", 1);
        writeMessage(out, hello);
    }

    /** The windows between {@code WINDOW_OPENED} and {@code WINDOW_CLOSED}: the id registry. */
    private static List<Object> registeredWindows() {
        List<Object> windows = new ArrayList<>();
        for (Map.Entry<String, Window> entry : windowsById.entrySet()) {
            windows.add(describeWindow(entry.getValue(), entry.getKey()));
        }
        return windows;
    }

    /** Starts the queue-to-socket writer thread; {@link #install} does this in production. */
    static void startWriter() {
        startDaemon(EventBridge::writerLoop, "ibcontroller-event-writer");
    }
}

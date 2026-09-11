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
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * {@code AWTEventListener} install and NDJSON event forwarding over the event socket (Java
 * Agent document sec 04, sec 05, sec 07). Currently {@code window_opened}/{@code window_closed}
 * only -- the 2FA relabel detection (poll or {@code PropertyChangeListener}) is a separate,
 * still-open question, Phase 1 step 7.
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
     * Assigns each open window an opaque, stable ID -- the direct analog of IBC's own
     * {@code WindowHandler} callbacks receiving the literal {@code Window} object reference
     * (checked against IBC's real source, 2026-09-06: {@code SwingUtils.findButton}/
     * {@code findCheckBox}/{@code findTextField} etc. all take an explicit container/window
     * reference, never search across every open window). Our own JSON-over-socket boundary
     * can't hand Python a live Java object, so this is the next best thing: an ID Python can
     * echo back on a later command, which {@link #resolveWindow} turns back into the literal
     * object -- {@code WriteOps} then scopes its component search to just that window, instead
     * of the old {@code ComponentLookup.findByAccessibleName}'s global {@code Window.getWindows()}
     * scan (the confirmed root cause of a real, live-caught bug, 2026-09-06: a declarative
     * dismiss rule's "OK" click landed on the wrong of two simultaneously-open "OK" buttons).
     *
     * <p>{@code Window}/{@code Component} don't override {@code equals}/{@code hashCode}, so a
     * plain identity comparison is already what a {@code Map} keyed on them gives us -- no need
     * for {@code IdentityHashMap} specifically. {@code ConcurrentHashMap} (both directions, kept
     * in sync together) because {@link #registerWindow}/{@link #unregisterWindow} run on the EDT
     * ({@code onAwtEvent}, guaranteed by the JDK) while {@link #resolveWindow} is read from the
     * command-socket thread ({@code Protocol.handleCommandClient}, via {@code WriteOps}) and
     * {@link #sendSnapshot} from the event-socket thread -- three different threads, genuinely
     * concurrent, unlike the plain {@code HashMap}s the rest of this class otherwise uses for
     * single-threaded, per-message construction.
     */
    private static final Map<Window, String> windowIds = new ConcurrentHashMap<>();

    private static final Map<String, Window> windowsById = new ConcurrentHashMap<>();
    private static final AtomicLong nextWindowId = new AtomicLong(1);

    private static volatile PrintWriter currentWriter;

    private EventBridge() {}

    /**
     * Installed once, at agent startup, before the target app creates any window -- the IBC
     * ordering lesson (Java Agent document sec 01/04). Must be called before the entry point is
     * launched, not after; {@link AgentMain} enforces that ordering, not this method.
     */
    static void install() {
        Toolkit.getDefaultToolkit()
                .addAWTEventListener(EventBridge::onAwtEvent, AWTEvent.WINDOW_EVENT_MASK);
        startDaemon(EventBridge::writerLoop, "ibcontroller-event-writer");
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
        String id = "window_opened".equals(kind) ? registerWindow(window) : unregisterWindow(window);
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("type", "event");
        message.put("seq", seq.incrementAndGet());
        message.put("kind", kind);
        message.put("window", describeWindow(window, id));
        if (!queue.offer(message)) {
            overflowed.set(true);
            LOG.warning("event queue full (" + QUEUE_CAPACITY + "): dropped window event "
                    + kind + " " + window.getClass().getName());
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
            currentWriter = out;
            LOG.fine("event client connected");
            sendHello(out);
            sendSnapshot(out);
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

    private static void sendHello(PrintWriter out) {
        Map<String, Object> hello = new LinkedHashMap<>();
        hello.put("type", "hello");
        hello.put("protocol_version", 1);
        writeMessage(out, hello);
    }

    private static void sendSnapshot(PrintWriter out) {
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("type", "snapshot");
        List<Object> windows = new ArrayList<>();
        for (Window window : Window.getWindows()) {
            if (window.isDisplayable()) {
                windows.add(describeWindow(window, windowIds.get(window)));
            }
        }
        snapshot.put("windows", windows);
        writeMessage(out, snapshot);
    }
}

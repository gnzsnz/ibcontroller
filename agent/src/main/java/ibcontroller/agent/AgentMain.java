package ibcontroller.agent;

import java.io.IOException;
import java.lang.reflect.Method;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.channels.ClosedChannelException;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.logging.ConsoleHandler;
import java.util.logging.FileHandler;
import java.util.logging.Level;
import java.util.logging.Logger;
import java.util.logging.SimpleFormatter;
import java.util.function.Consumer;

/**
 * Entry point (Java Agent document, sec 01-02).
 *
 * <p>Becomes the JVM's {@code main()}, like IBC -- reflectively invokes TWS/Gateway's own entry
 * point ({@code ibgateway.GWClient} or {@code jclient.LoginFrame}) after both sockets are up.
 * The classpath that makes those classes visible is assembled by {@code launcher.py}, not by
 * this class -- this project never bundles Gateway/TWS's jars itself.
 *
 * <p>Deliberately not replicated: IBC's command-line credential passthrough. ibcontroller
 * drives login through the agent's own commands ({@code set_text}/{@code click}, WriteOps), not
 * command-line arguments, so the entry point is always invoked with no arguments -- Gateway/TWS
 * shows its own ordinary login window.
 *
 * <p>{@link EventBridge#install} runs before {@link #launchEntryPoint}: the listener must exist
 * before the target app creates any window, or the first one is missed.
 *
 * <p>{@link #launchEntryPoint} runs on its own thread, not inline in {@code main()}:
 * {@code jclient.LoginFrame.main()} may not return promptly, and the command accept loop must
 * not wait on it. IBC's {@code IbcTws.load()} likewise starts its command server before invoking
 * the entry point.
 */
public final class AgentMain {

    private static final Logger LOG = Logger.getLogger(AgentMain.class.getName());
    private static final long ACCEPT_RETRY_MS = 100;

    private AgentMain() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println(
                    "Usage: AgentMain <command-socket-path> [entry-point-class]"
                            + " [entry-point-settings-dir]");
            System.exit(1);
            return;
        }
        String commandSocketPath = args[0];
        String entryPointClass = args.length > 1 ? args[1] : null;
        // Passed to the entry point's main(String[]) as args[0]: jclient.LoginFrame.main() (TWS)
        // exits silently without it, even with -DjtsConfigDir. IBC passes it to both entry points
        // (startTws()/startGateway(): twsArgs[0] = getTWSSettingsDirectory()), so this does too.
        String[] entryPointArgs =
                args.length > 2 ? new String[] {args[2]} : new String[0];
        String eventSocketPath = deriveEventSocketPath(commandSocketPath);

        configureLogging();

        ServerSocketChannel commandServer = bind(commandSocketPath);
        LOG.info("command socket listening on " + commandSocketPath);
        ServerSocketChannel eventServer = bind(eventSocketPath);
        LOG.info("event socket listening on " + eventSocketPath);

        EventBridge.install();

        Thread eventAcceptThread =
                new Thread(
                        () -> acceptLoop(eventServer, EventBridge::handleEventClient),
                        "ibcontroller-event-accept");
        eventAcceptThread.setDaemon(true);
        eventAcceptThread.start();

        if (entryPointClass != null) {
            // Own thread: jclient.LoginFrame.main() (TWS) may not return promptly, and the
            // command accept loop below must not wait on it (as with the event-accept thread).
            Thread entryPointThread =
                    new Thread(
                            () -> {
                                try {
                                    launchEntryPoint(entryPointClass, entryPointArgs);
                                } catch (Exception e) {
                                    LOG.log(
                                            Level.SEVERE,
                                            "entry point launch failed: " + e.getMessage(),
                                            e);
                                }
                            },
                            "ibcontroller-entry-point");
            entryPointThread.setDaemon(true);
            entryPointThread.start();
        }

        try {
            // Single-threaded, one client at a time -- the only client is ibcontroller's own
            // dispatch.py, never more than one connection in practice (Java Agent document sec 09).
            acceptLoop(commandServer, Protocol::handleCommandClient);
        } finally {
            commandServer.close();
            eventServer.close();
            Files.deleteIfExists(Path.of(commandSocketPath));
            Files.deleteIfExists(Path.of(eventSocketPath));
        }
    }

    /**
     * Configures java.util.logging from JVM system properties set by
     * {@code launcher.py}: {@code -Dibcontroller.logfile=<path>} routes
     * agent-side logs to a dedicated per-instance file (separate from Python's own
     * {@code ibcontroller-{instance}.log} and from Gateway's stdout drain, so the
     * three streams never interleave); {@code -Dibcontroller.log.level=<LEVEL>}
     * mirrors Python's own {@code Config.log_level} (INFO default). Both are set by
     * Python and read here -- the agent never invents paths or levels itself. When
     * {@code ibcontroller.logfile} is absent (e.g. {@code make run}), logs go to
     * stderr only.
     *
     * <p>Rotation: {@code -Dibcontroller.logfile.maxbytes=<N>} and
     * {@code -Dibcontroller.logfile.backupcount=<N>} mirror Python's own
     * {@code log_max_bytes}/{@code log_backup_count}. {@code FileHandler}'s own
     * fd can't be rotated externally (the JVM keeps writing to a renamed/unlinked
     * inode), so this uses its 4-arg constructor, which does native generation
     * rotation on the JVM's own thread. {@code maxbytes=0} (the default if the
     * property is absent) keeps the old unbounded, append-only file.
     */
    private static void configureLogging() throws Exception {
        Logger root = Logger.getLogger("");
        root.setUseParentHandlers(false);

        String levelName = System.getProperty("ibcontroller.log.level", "INFO");
        Level level = parseLevel(levelName);
        root.setLevel(level);
        LOG.setLevel(level);

        String logFile = System.getProperty("ibcontroller.logfile");
        if (logFile != null && !logFile.isEmpty()) {
            int maxBytes = Integer.parseInt(
                    System.getProperty("ibcontroller.logfile.maxbytes", "0"));
            int backupCount = Integer.parseInt(
                    System.getProperty("ibcontroller.logfile.backupcount", "0"));
            try {
                FileHandler fileHandler =
                        maxBytes > 0
                                ? new FileHandler(logFile, maxBytes, Math.max(backupCount, 1), true)
                                : new FileHandler(logFile, true);
                fileHandler.setFormatter(new SimpleFormatter());
                root.addHandler(fileHandler);
                return;
            } catch (IOException e) {
                // A logging problem must never stop the agent before it binds its sockets.
                root.addHandler(new ConsoleHandler());
                LOG.warning("cannot open log file " + logFile + ", logging to console: " + e);
                return;
            }
        }
        root.addHandler(new ConsoleHandler());
    }

    private static Level parseLevel(String name) {
        return switch (name.toUpperCase()) {
            case "DEBUG" -> Level.FINE;
            case "INFO" -> Level.INFO;
            case "WARNING", "WARN" -> Level.WARNING;
            case "ERROR", "CRITICAL" -> Level.SEVERE;
            default -> Level.INFO;
        };
    }

    /**
     * Refuses to start rather than take over a live socket path: two agents sharing an
     * {@code --instance} name would otherwise let the second unlink the first's socket file,
     * leaving the first unreachable with no error. Applies to the command and event sockets.
     */
    private static ServerSocketChannel bind(String socketPath) throws Exception {
        Path path = Path.of(socketPath);
        if (Files.exists(path)) {
            if (isAlive(path)) {
                throw new IllegalStateException(
                        "Refusing to start: "
                                + socketPath
                                + " already has a live listener -- another ibcontroller"
                                + " agent instance is using this --instance name. Use a"
                                + " different --instance, or stop that instance first.");
            }
            // Stale leftover from an unclean shutdown: nothing is listening, so reuse the path.
            Files.delete(path);
        }
        ServerSocketChannel server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
        server.bind(UnixDomainSocketAddress.of(path));
        return server;
    }

    /**
     * Checks by attempting a client connection: a Unix domain socket's file can outlive the
     * process that created it, so its existence proves nothing. A successful connect means
     * something is listening; any failure means nothing is.
     */
    private static boolean isAlive(Path path) {
        try (SocketChannel probe = SocketChannel.open(StandardProtocolFamily.UNIX)) {
            probe.connect(UnixDomainSocketAddress.of(path));
            return true;
        } catch (java.io.IOException e) {
            return false;
        }
    }

    static void acceptLoop(ServerSocketChannel server, Consumer<SocketChannel> handler) {
        while (server.isOpen()) {
            SocketChannel client;
            try {
                client = server.accept();
            } catch (ClosedChannelException e) {
                return; // server socket closed: the only fatal condition
            } catch (java.io.IOException e) {
                LOG.log(Level.SEVERE, "accept error, retrying: " + e.getMessage(), e);
                try {
                    Thread.sleep(ACCEPT_RETRY_MS); // avoid a hot loop on a persistent error
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return;
                }
                continue;
            }
            // Throwable, not Exception: a failed client must never end the loop (and, for the
            // command socket, delete both socket files in main's finally).
            try (client) {
                handler.accept(client);
            } catch (Throwable t) {
                LOG.log(Level.SEVERE, "client handler failed: " + t, t);
            }
        }
    }

    /** {@code <prefix>-cmd.sock} -> {@code <prefix>-events.sock} (Java Agent document sec 06:
     * two sockets, not one multiplexed). Falls back to a suffix append for a command-socket
     * path that doesn't follow that convention (e.g. an ad hoc {@code --socket=} override). */
    private static String deriveEventSocketPath(String commandSocketPath) {
        String suffix = "-cmd.sock";
        if (commandSocketPath.endsWith(suffix)) {
            return commandSocketPath.substring(0, commandSocketPath.length() - suffix.length())
                    + "-events.sock";
        }
        return commandSocketPath + ".events";
    }

    /**
     * Reflective, not a compile-time dependency: {@code entryPointClass} only exists on the
     * classpath once launched with Gateway/TWS's own jars on it
     * ({@code scripts/launch-agent.sh}) -- this project's own build never depends on them.
     */
    private static void launchEntryPoint(String entryPointClass, String[] entryPointArgs)
            throws Exception {
        Class<?> clazz = Class.forName(entryPointClass);
        Method main = clazz.getMethod("main", String[].class);
        LOG.info("launching " + entryPointClass + ".main()");
        main.invoke(null, (Object) entryPointArgs);
    }
}

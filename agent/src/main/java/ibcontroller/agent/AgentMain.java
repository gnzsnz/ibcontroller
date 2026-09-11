package ibcontroller.agent;

import java.lang.reflect.Method;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
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
 * Entry point (Java Agent document, sec 01-02; CLAUDE.md's Build plan, Phase 1).
 *
 * <p>Phase 1 step 3: becomes the JVM's {@code main()}, like IBC (sec 02, decided: B) --
 * reflectively invokes TWS/Gateway's own entry point ({@code ibgateway.GWClient} or
 * {@code jclient.LoginFrame}) after both sockets are up. The classpath that makes those classes
 * visible at all is assembled by {@code scripts/launch-agent.sh} (this phase's stand-in for
 * Python's future {@code launcher.py}, grounded in IBC's own
 * {@code resources/scripts/ibcstart.sh}), not by this class -- this project never bundles
 * Gateway/TWS's jars itself.
 *
 * <p>Deliberately not replicated: IBC's command-line credential passthrough. ibcontroller
 * drives login through the agent's own commands ({@code set_text}/{@code click}, WriteOps), not
 * command-line arguments, so the entry point is always invoked with no arguments -- Gateway/TWS
 * shows its own ordinary login window.
 *
 * <p>Phase 1 step 6: {@link EventBridge#install} runs before {@link #launchEntryPoint} below --
 * the IBC ordering lesson (the listener must be installed before the target app creates any
 * window, or the first one is missed).
 *
 * <p>2026-09-09: {@link #launchEntryPoint} runs on its own thread, not inline in {@code main()} --
 * found live-testing TWS (not Gateway): {@code jclient.LoginFrame.main()} doesn't reliably return
 * within a normal ready-timeout window the way {@code ibgateway.GWClient.main()} does, and the
 * command accept loop must not be gated behind it returning. Matches IBC's own real ordering
 * ({@code IbcTws.load()} starts its command server before invoking its TWS/Gateway entry point)
 * and the event-accept thread's existing "don't gate on the entry point" shape, just applied to
 * the command socket too.
 */
public final class AgentMain {

    private static final Logger LOG = Logger.getLogger(AgentMain.class.getName());

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
        // Passed through to the entry point's own main(String[]) as args[0] -- confirmed
        // live, 2026-09-09: jclient.LoginFrame.main() (TWS) silently exits(0) within under a
        // second with no window at all unless its settings directory is given this way, not
        // just via -DjtsConfigDir; ibgateway.GWClient.main() tolerates its absence but IBC's
        // own IbcTws.java passes it identically to both (startTws()/startGateway(), each doing
        // twsArgs[0] = getTWSSettingsDirectory()), so this matches IBC's real call shape for
        // both entry points rather than special-casing TWS alone.
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
            // Run in its own thread rather than blocking main() here -- GWClient.main() returns
            // fast enough that this was never observed, but jclient.LoginFrame.main() (TWS) does
            // not reliably return within a normal ready-timeout window, and the command accept
            // loop below must not wait on it: IBC's own IbcTws.load() starts its command server
            // before invoking its TWS/Gateway entry point for exactly this reason (checked
            // directly, 2026-09-09), and this project's own event-accept thread already follows
            // that same "don't gate on the entry point" rule -- this brings the command socket in
            // line with it.
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
     * {@code launcher.py} (2026-09-08): {@code -Dibcontroller.logfile=<path>} routes
     * agent-side logs to a dedicated per-instance file (separate from Python's own
     * {@code ibcontroller-{instance}.log} and from Gateway's stdout drain, so the
     * three streams never interleave); {@code -Dibcontroller.log.level=<LEVEL>}
     * mirrors Python's own {@code Config.log_level} (INFO default). Both are set by
     * Python and read here -- the agent never invents paths or levels itself. When
     * {@code ibcontroller.logfile} is absent (e.g. {@code make run}), logs go to
     * stderr only, same as before this mechanism existed.
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
            FileHandler fileHandler = new FileHandler(logFile, true);
            fileHandler.setFormatter(new SimpleFormatter());
            root.addHandler(fileHandler);
        } else {
            ConsoleHandler consoleHandler = new ConsoleHandler();
            root.addHandler(consoleHandler);
        }
    }

    private static Level parseLevel(String name) {
        return switch (name.toUpperCase()) {
            case "DEBUG" -> Level.FINE;
            case "INFO" -> Level.INFO;
            case "WARNING", "WARN" -> Level.WARNING;
            case "ERROR" -> Level.SEVERE;
            default -> Level.INFO;
        };
    }

    /**
     * Refuses to start rather than silently taking over an already-live socket path --
     * confirmed 2026-09-05: the previous version of this method unconditionally
     * {@code deleteIfExists}'d the path first, so reusing the same {@code --instance}
     * name for two concurrently-running agents (e.g. launching both live and paper
     * with {@code --instance=prod} instead of two distinct names) let the second one
     * silently unlink the first's socket file and bind its own in its place -- the
     * first agent's JVM kept running, but its command socket became unreachable via
     * that shared path, with no error raised anywhere. Same treatment applies to the
     * event socket, for free, since both go through this one method.
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
            // Stale leftover from an unclean shutdown (the owning process died before
            // reaching main()'s own cleanup) -- confirmed above that nothing is
            // listening, so it's safe to remove and take the path over ourselves.
            Files.delete(path);
        }
        ServerSocketChannel server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
        server.bind(UnixDomainSocketAddress.of(path));
        return server;
    }

    /**
     * Confirmed by attempting a real client connection, not by inspecting the file
     * alone -- a Unix domain socket's directory entry can outlive the process that
     * created it (exactly the unclean-shutdown case above), so the file merely
     * existing proves nothing on its own. Connecting successfully means something is
     * actually listening; any failure (connection refused, or anything else) means
     * it isn't.
     */
    private static boolean isAlive(Path path) {
        try (SocketChannel probe = SocketChannel.open(StandardProtocolFamily.UNIX)) {
            probe.connect(UnixDomainSocketAddress.of(path));
            return true;
        } catch (java.io.IOException e) {
            return false;
        }
    }

    private static void acceptLoop(ServerSocketChannel server, Consumer<SocketChannel> handler) {
        while (true) {
            try (SocketChannel client = server.accept()) {
                handler.accept(client);
            } catch (java.io.IOException e) {
                LOG.log(Level.SEVERE, "accept loop error: " + e.getMessage(), e);
                return;
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

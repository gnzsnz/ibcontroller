package ibcontroller.agent;

import java.io.IOException;
import java.net.SocketAddress;
import java.net.SocketOption;
import java.nio.channels.ClosedChannelException;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.channels.spi.SelectorProvider;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.logging.Handler;
import java.util.logging.Level;
import java.util.logging.LogRecord;
import java.util.logging.Logger;
import javax.swing.JButton;
import javax.swing.SwingUtilities;

/** Dependency-free unit tests for the agent; run with {@code make test}. */
public final class AgentTests {

    private static int failures;

    public static void main(String[] args) {
        run("parser accepts nesting up to the limit", AgentTests::parserAcceptsLimit);
        run("parser rejects deeper nesting", AgentTests::parserRejectsDeep);
        run("accept loop survives errors", AgentTests::acceptLoopSurvives);
        run("deferred click failure is logged", AgentTests::deferredClickLogged);
        if (failures > 0) {
            System.out.println(failures + " test(s) failed");
            System.exit(1);
        }
        System.out.println("all tests passed");
    }

    private static void run(String name, Runnable test) {
        try {
            test.run();
            System.out.println("ok   " + name);
        } catch (Throwable t) {
            failures++;
            System.out.println("FAIL " + name + ": " + t);
        }
    }

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    private static void parserAcceptsLimit() {
        Protocol.Json.parse("[".repeat(64) + "]".repeat(64));
    }

    private static void parserRejectsDeep() {
        try {
            Protocol.Json.parse("[".repeat(100_000) + "]".repeat(100_000));
        } catch (IllegalArgumentException e) {
            return;
        }
        throw new AssertionError("deep nesting was not rejected");
    }

    /** Scripted accept results: IOException, a handler that throws Error, a normal client, close. */
    private static void acceptLoopSurvives() {
        FakeServer server = new FakeServer();
        List<Integer> handled = new ArrayList<>();
        AgentMain.acceptLoop(
                server,
                client -> {
                    handled.add(handled.size());
                    if (handled.size() == 1) {
                        throw new StackOverflowError("simulated");
                    }
                });
        check(server.accepts == 4, "loop stopped early: " + server.accepts + " accepts");
        check(handled.size() == 2, "handler calls: " + handled.size());
    }

    private static void deferredClickLogged() {
        Logger logger = Logger.getLogger(WriteOps.class.getName());
        List<LogRecord> records = new ArrayList<>();
        Handler capture =
                new Handler() {
                    @Override
                    public void publish(LogRecord record) {
                        records.add(record);
                    }

                    @Override
                    public void flush() {}

                    @Override
                    public void close() {}
                };
        logger.addHandler(capture);
        try {
            JButton button =
                    new JButton() {
                        @Override
                        public void doClick() {
                            throw new IllegalStateException("disposed");
                        }
                    };
            WriteOps.deferClick(button, "OK");
            SwingUtilities.invokeAndWait(() -> {}); // EDT drains the deferred click first
        } catch (Exception e) {
            throw new AssertionError(e);
        } finally {
            logger.removeHandler(capture);
        }
        check(
                records.size() == 1
                        && records.get(0).getLevel() == Level.WARNING
                        && records.get(0).getMessage().contains("OK"),
                "expected one WARNING naming the target, got " + records.size());
    }

    /** Scripted server: accept 1 throws IOException, 2-3 return clients, 4 closes itself. */
    private static final class FakeServer extends ServerSocketChannel {
        int accepts;

        FakeServer() {
            super(SelectorProvider.provider());
        }

        @Override
        public SocketChannel accept() throws IOException {
            accepts++;
            switch (accepts) {
                case 1 -> throw new IOException("simulated accept failure");
                case 2, 3 -> {
                    return SocketChannel.open();
                }
                default -> {
                    close();
                    throw new ClosedChannelException();
                }
            }
        }

        @Override
        public ServerSocketChannel bind(SocketAddress local, int backlog) {
            return this;
        }

        @Override
        public <T> ServerSocketChannel setOption(SocketOption<T> name, T value) {
            return this;
        }

        @Override
        public <T> T getOption(SocketOption<T> name) {
            return null;
        }

        @Override
        public Set<SocketOption<?>> supportedOptions() {
            return Set.of();
        }

        @Override
        public java.net.ServerSocket socket() {
            throw new UnsupportedOperationException();
        }

        @Override
        public SocketAddress getLocalAddress() {
            return null;
        }

        @Override
        protected void implCloseSelectableChannel() {}

        @Override
        protected void implConfigureBlocking(boolean block) {}
    }
}

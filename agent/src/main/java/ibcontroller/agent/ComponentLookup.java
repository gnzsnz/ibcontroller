package ibcontroller.agent;

import java.awt.Component;
import java.awt.Container;
import java.awt.Window;
import java.lang.reflect.InvocationTargetException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.accessibility.AccessibleContext;
import javax.swing.SwingUtilities;
import javax.swing.text.JTextComponent;

/**
 * Read-only Swing tree walk (Java Agent document sec 05, sec 07's {@code dump}/{@code get_text}).
 *
 * <p>Every read happens on the EDT via {@code invokeAndWait} (sec 03) -- reads are as
 * EDT-sensitive as writes; Swing components should never be touched off the EDT.
 *
 * <p>{@code dump}'s shape is a flat list of components, not a nested tree -- matching jauto's own
 * {@code list_ui_components} precedent (Java Agent document sec 07, {@code poc/jauto_dump.py}).
 */
final class ComponentLookup {

    private ComponentLookup() {}

    /**
     * A real, live-caught crash (2026-09-09): {@code dump}'s {@code text} field used to be a
     * component's entire {@code getText()} with no cap at all -- fine for the short strings
     * every recogniser actually matches against ("This is not a brokerage account", a dialog
     * title, ...), but Gateway's own {@code File > Gateway Logs > View Logs} window (and,
     * separately, an oversized menu the same day) renders its whole log file into a single
     * {@code JTextComponent}. {@code recognisers.handle_window_opened} calls {@code dump}
     * unconditionally on every unprompted window (its own docstring already names a related,
     * narrower instance of this same class of bug -- aggregating multiple windows into one
     * dump, fixed by window-ID scoping, 2026-09-06) -- one such window with megabytes of log
     * text in it produced a JSON response line past Python's {@code asyncio} stream reader's
     * default 64 KiB limit, crashing the read loop outright (a real traceback, not a guess).
     * Capped here, at the source, rather than only raising the Python-side buffer limit --
     * the buffer bump (agent_client.py) is real defense in depth for many small components
     * summing past the limit, but only this cap actually bounds a single pathological field.
     * 2000 characters is generous for every real recogniser match (all well under a few
     * hundred characters) and small enough that even a window full of large text components
     * stays comfortably inside any reasonable line-length budget.
     */
    private static final int MAX_TEXT_LENGTH = 2000;

    /**
     * Dumps every displayable window, or only those whose title contains {@code titleFilter}
     * (case-insensitive) if it's non-null. Window-vs-window disambiguation belongs to a real
     * recogniser eventually (Python-side L5); this is the bootstrap version for empirical
     * exploration against a real Gateway.
     *
     * <p><b>2026-09-06, a real live-caught bug's fix:</b> a title filter can match more than
     * one window at once (the persisting login frame, the real main window, and a fresh popup
     * can all share "IBKR Gateway") -- on a live account, whose main window carries far more
     * real data than paper's, aggregating all of them into one response was large enough to
     * exceed the wire protocol's read-buffer limit outright, corrupting the shared command
     * connection and, with it, the graceful shutdown that needed the same connection
     * afterward. See {@link #dump(String, Window)} for the fix: scope to one resolved window
     * (from {@code EventBridge}'s registry) instead of matching by title at all, whenever a
     * caller has a `window_id` to give -- the same mechanism already built for
     * {@link WriteOps}'s write commands, extended to this read one.
     */
    static List<Object> dump(String titleFilter) {
        return dump(titleFilter, null);
    }

    /** Scoped to `scope` alone when given -- ignores `titleFilter` entirely in that case,
     * since a resolved window is strictly more precise than a title match could ever be.
     * `scope == null` keeps the original title-filtered-or-global scan. See this method's
     * unscoped overload for the bug this exists to fix. */
    static List<Object> dump(String titleFilter, Window scope) {
        List<Object> result = new ArrayList<>();
        runOnEdt(
                () -> {
                    if (scope != null) {
                        collect(scope, null, result);
                        return;
                    }
                    for (Window window : Window.getWindows()) {
                        if (!window.isDisplayable()) {
                            continue;
                        }
                        if (titleFilter != null && !matchesTitle(window, titleFilter)) {
                            continue;
                        }
                        collect(window, null, result);
                    }
                });
        return result;
    }

    private static boolean matchesTitle(Window window, String titleFilter) {
        String title = extractTitle(window);
        return title != null && title.toLowerCase().contains(titleFilter.toLowerCase());
    }

    /**
     * Confirmed 2026-09-05 (real Gateway 10.50, the "This is not a brokerage account"
     * warning dialog): the previous version of this method only checked {@code
     * JFrame}/{@code Frame}, never {@code JDialog}/{@code Dialog} -- {@code
     * window instanceof JFrame} is false for any dialog, so {@code title} stayed {@code
     * null} regardless of the dialog's actual title, and {@code dump}'s window filter
     * silently matched zero components for any dialog window, every time. {@code
     * Frame}/{@code Dialog} (the AWT base classes both {@code JFrame}/{@code JDialog}
     * extend) each have their own {@code getTitle()}, so checking those two covers both
     * Swing and plain-AWT windows of either kind uniformly.
     */
    private static String extractTitle(Window window) {
        if (window instanceof java.awt.Frame frame) {
            return frame.getTitle();
        }
        if (window instanceof java.awt.Dialog dialog) {
            return dialog.getTitle();
        }
        return null;
    }

    private static void collect(Component component, Component parent, List<Object> out) {
        out.add(describe(component, parent));
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) {
                collect(child, component, out);
            }
        }
    }

    // Diagnostic fields (showing, visible, parent_class) added 2026-09-04 after set_text landed
    // in the wrong (FIX Login) Username/Password pair despite isShowing() preference -- that
    // assumption was wrong, or incomplete, and needs real data to replace with something correct,
    // not a second guess.
    private static Map<String, Object> describe(Component component, Component parent) {
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("class", component.getClass().getName());
        String name = component.getName();
        if (name != null) {
            entry.put("name", name);
        }
        String accessibleName = accessibleName(component);
        if (accessibleName != null) {
            entry.put("accessible_name", accessibleName);
        }
        boolean isCredential = Security.isCredentialField(component);
        entry.put("credential_field", isCredential);
        entry.put("enabled", component.isEnabled());
        entry.put("showing", component.isShowing());
        entry.put("visible", component.isVisible());
        if (component instanceof javax.swing.AbstractButton button) {
            entry.put("selected", button.isSelected());
        }
        if (parent != null) {
            entry.put("parent_class", parent.getClass().getName());
        }
        String rawText = rawText(component);
        if (rawText != null) {
            if (isCredential) {
                entry.put("text", Security.redactedPlaceholder(rawText.length()));
            } else if (rawText.length() > MAX_TEXT_LENGTH) {
                entry.put("text", rawText.substring(0, MAX_TEXT_LENGTH));
                entry.put("text_truncated", true);
                entry.put("text_length", rawText.length());
            } else {
                entry.put("text", rawText);
            }
        }
        return entry;
    }

    private static String accessibleName(Component component) {
        AccessibleContext context = component.getAccessibleContext();
        return context != null ? context.getAccessibleName() : null;
    }

    private static String rawText(Component component) {
        if (!(component instanceof JTextComponent textComponent)) {
            return null;
        }
        return textComponent.getText();
    }

    /**
     * Looks up a component by accessible name and returns its text, refusing outright on
     * credential fields (Security, sec 08) rather than redacting -- stricter than {@link
     * #dump}, which shows a length-only placeholder for diagnostics.
     *
     * <p>Confirmed empirically (2026-09-04, real Gateway 10.50 login window) that more than one
     * component can share an accessible name -- Gateway's login window carries both an IB API
     * and a FIX Login section's Username/Password fields in the same tree, only one actually
     * showing at a time. Prefers a currently-{@code isShowing()} match over the first match in
     * document order for exactly that reason -- not yet proven this always picks the right one,
     * only that picking blindly by document order would sometimes be wrong.
     */
    static String getText(String target) {
        String[] result = new String[1];
        RuntimeException[] error = new RuntimeException[1];
        runOnEdt(
                () -> {
                    try {
                        Component component = findByAccessibleName(target);
                        if (Security.isCredentialField(component)) {
                            error[0] = new CredentialRefusedException(target);
                            return;
                        }
                        if (!(component instanceof JTextComponent textComponent)) {
                            error[0] = new ElementNotFoundException(target);
                            return;
                        }
                        result[0] = textComponent.getText();
                    } catch (ElementNotFoundException e) {
                        error[0] = e;
                    }
                });
        if (error[0] != null) {
            throw error[0];
        }
        return result[0];
    }

    static Component findByAccessibleName(String target) {
        return findByAccessibleName(target, null);
    }

    /**
     * Scoped to {@code scope}'s own component tree when given, matching every one of IBC's own
     * lookup helpers (checked directly against IBC's source, 2026-09-06: {@code
     * SwingUtils.findButton}/{@code findCheckBox}/{@code findTextField} etc. all take an explicit
     * container reference, never search across every open window) -- {@code scope == null} keeps
     * this method's original global {@code Window.getWindows()} scan, for callers that don't yet
     * have a window ID to pass.
     *
     * <p>This scoping is what a real, live-caught bug needed (2026-09-06): a declarative dismiss
     * rule's "OK" click, resolved via the old unscoped search, landed on a different, simultaneously-
     * showing "OK" button belonging to the Global Configuration dialog (which the same commit had
     * just been told to close, but hadn't finished disposing yet) instead of the actual popup it was
     * meant to dismiss -- two windows, two "OK"-labeled buttons, no way to tell them apart without a
     * scope.
     */
    static Component findByAccessibleName(String target, Window scope) {
        if (scope != null) {
            Component firstMatch = null;
            for (Component candidate : matchesByAccessibleName(scope, target)) {
                if (candidate.isShowing()) {
                    return candidate;
                }
                if (firstMatch == null) {
                    firstMatch = candidate;
                }
            }
            if (firstMatch != null) {
                return firstMatch;
            }
            throw new ElementNotFoundException(target, collectAccessibleNames(scope));
        }
        Component firstMatch = null;
        for (Window window : Window.getWindows()) {
            if (!window.isDisplayable()) {
                continue;
            }
            for (Component candidate : matchesByAccessibleName(window, target)) {
                if (candidate.isShowing()) {
                    return candidate;
                }
                if (firstMatch == null) {
                    firstMatch = candidate;
                }
            }
        }
        if (firstMatch != null) {
            return firstMatch;
        }
        List<String> allNames = new ArrayList<>();
        for (Window window : Window.getWindows()) {
            if (window.isDisplayable()) {
                allNames.addAll(collectAccessibleNames(window));
            }
        }
        throw new ElementNotFoundException(target, allNames);
    }

    private static List<Component> matchesByAccessibleName(Component component, String target) {
        List<Component> matches = new ArrayList<>();
        collectMatches(component, target, matches);
        return matches;
    }

    private static void collectMatches(Component component, String target, List<Component> out) {
        if (target.equals(accessibleName(component))) {
            out.add(component);
        }
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) {
                collectMatches(child, target, out);
            }
        }
    }

    /**
     * Collects every accessible name in a component tree -- for enriching error
     * messages when a target isn't found (2026-09-08). Mirrors the traversal
     * shape of {@code collectMatches} but only records names, not components.
     * Case-insensitive, matching {@code findByAccessibleName}'s own matching
     * convention.
     */
    static List<String> collectAccessibleNames(Component root) {
        List<String> names = new ArrayList<>();
        collectNames(root, names);
        return names;
    }

    private static void collectNames(Component component, List<String> out) {
        String name = accessibleName(component);
        if (name != null && !name.isEmpty()) {
            out.add(name);
        }
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) {
                collectNames(child, out);
            }
        }
    }

    static final class ElementNotFoundException extends RuntimeException {
        ElementNotFoundException(String target) {
            super("no component with accessible name " + target);
        }

        ElementNotFoundException(String target, List<String> availableNames) {
            super("no component with accessible name " + target
                    + "; available names: " + availableNames);
        }
    }

    static final class CredentialRefusedException extends RuntimeException {
        CredentialRefusedException(String target) {
            super(target + " is a credential field");
        }
    }

    /** A caller passed a {@code window_id} that no longer resolves to a live window -- it
     * closed between Python recognising it and this command actually reaching the agent. A
     * real, expected race (EventBridge.java's own docstring), not a bug -- distinguished from
     * {@link ElementNotFoundException} (a window's own component tree doesn't have what was
     * asked for) since the caller's own retry/error-handling logic differs: a gone window means
     * "nothing left to act on here", not "check the label spelling". */
    static final class WindowGoneException extends RuntimeException {
        WindowGoneException(String windowId) {
            super("window " + windowId + " is no longer open");
        }
    }

    static void runOnEdt(Runnable action) {
        if (SwingUtilities.isEventDispatchThread()) {
            action.run();
            return;
        }
        try {
            SwingUtilities.invokeAndWait(action::run);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException("EDT dispatch interrupted", e);
        } catch (InvocationTargetException e) {
            // Re-throw the action's own unchecked exception unchanged, not wrapped -- since
            // 2026-09-08 findByName throws from inside the EDT lambda, and Protocol.java's
            // error mapping relies on the exception type (ElementNotFoundException etc.)
            // surviving the EDT boundary. Errors and checked Throwables (impossible for a
            // Runnable) keep the original generic re-wrap, so Protocol's `catch
            // (RuntimeException)` still catches everything the way it always did.
            Throwable cause = e.getCause();
            if (cause instanceof RuntimeException runtimeException) {
                throw runtimeException;
            }
            throw new RuntimeException("EDT dispatch failed", cause);
        }
    }
}

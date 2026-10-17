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
 * <p>{@code dump}'s shape is a flat list of components, not a nested tree.
 */
final class ComponentLookup {

    private ComponentLookup() {}

    /**
     * Cap on a {@code dump} {@code text} field. Some windows (such as the log viewer) render a
     * whole file into one {@code JTextComponent}, which would push a response line past the
     * client's stream read limit. Only this cap bounds a single oversized field; the client's
     * larger buffer covers many small components summing past the limit. Recogniser matches
     * are far shorter than 2000 characters.
     */
    private static final int MAX_TEXT_LENGTH = 2000;

    /**
     * Dumps every displayable window, or only those whose title contains {@code titleFilter}
     * (case-insensitive) if it's non-null.
     *
     * <p>A title filter can match several windows at once (the login frame, the main window and
     * a popup can share a title), and aggregating them can exceed the client's read limit. Prefer
     * {@link #dump(String, Window)}, scoped to one window resolved from {@code EventBridge}'s
     * registry.
     */
    static List<Object> dump(String titleFilter) {
        return dump(titleFilter, null);
    }

    /** Scoped to `scope` alone when given, ignoring `titleFilter` (a resolved window is more
     * precise than a title match). `scope == null` gives the title-filtered-or-global scan. */
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
     * Title of a frame or dialog, {@code null} for other windows. Checks the AWT base classes
     * {@code Frame}/{@code Dialog}, which each have their own {@code getTitle()}, so Swing and
     * plain-AWT windows of either kind are covered.
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

    // showing, visible and parent_class let a caller tell apart components that share a name.
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
     * #dump}, which shows a length-only placeholder for diagnostics. Searches only {@code
     * scope}'s tree when given, every open window when {@code null}.
     *
     * <p>More than one component can share an accessible name (Gateway's login window holds
     * both an IB API and a FIX Login Username/Password pair, one showing at a time), so a
     * currently-{@code isShowing()} match is preferred over the first in document order.
     */
    static String getText(String target, Window scope) {
        String[] result = new String[1];
        RuntimeException[] error = new RuntimeException[1];
        runOnEdt(
                () -> {
                    try {
                        Component component = findByAccessibleName(target, scope);
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
     * Scoped to {@code scope}'s own component tree when given, as IBC's lookup helpers are
     * ({@code SwingUtils.findButton}/{@code findCheckBox}/{@code findTextField} take an explicit
     * container). {@code scope == null} scans every {@code Window.getWindows()}.
     *
     * <p>Scoping matters when two windows show buttons with the same label (for example "OK"):
     * an unscoped search can pick the wrong one.
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
     * Collects every accessible name in a component tree, to list the available names in a
     * not-found error. Same traversal as {@code collectMatches}, recording names only.
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
     * closed between Python recognising it and this command actually reaching the agent. An
     * expected race (see EventBridge.java), not a bug -- distinguished from
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
            // Re-throw the action's own unchecked exception unchanged: Protocol.java's error
            // mapping relies on the exception type (ElementNotFoundException etc.) surviving
            // the EDT boundary. Anything else is wrapped, so `catch (RuntimeException)` still
            // catches everything.
            Throwable cause = e.getCause();
            if (cause instanceof RuntimeException runtimeException) {
                throw runtimeException;
            }
            throw new RuntimeException("EDT dispatch failed", cause);
        }
    }
}

package ibcontroller.agent;

import java.awt.Component;
import java.awt.Container;
import java.awt.Window;
import java.util.ArrayList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.accessibility.AccessibleContext;
import javax.swing.AbstractButton;
import javax.swing.JDialog;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JMenuBar;
import javax.swing.JMenuItem;
import javax.swing.JTextField;
import javax.swing.JTree;
import javax.swing.MenuElement;
import javax.swing.SwingUtilities;
import javax.swing.text.JTextComponent;
import javax.swing.tree.TreeModel;
import javax.swing.tree.TreePath;

/**
 * {@code SET_TEXT}/{@code CLICK}/{@code SET_CHECKBOX}, EDT-dispatched (Java Agent document sec
 * 03, sec 05, sec 07).
 *
 * <p>{@code SET_TEXT}/{@code SET_CHECKBOX} use {@code invokeAndWait}
 * ({@link ComponentLookup#runOnEdt}) -- neither can open a modal dialog, so the caller can trust
 * the response immediately. {@code CLICK}'s lookup is still {@code invokeAndWait} (so a missing
 * target fails fast), but the actual {@code doClick()} is {@code invokeLater} -- it may open a
 * modal that blocks the EDT, so waiting synchronously for it risks deadlock; the caller confirms
 * the effect via an event or a follow-up read, not this response.
 *
 * <p>No credential restriction here, unlike {@code get_text}/{@code dump} (Security, sec 08):
 * filling username/password is core to the login flow this agent exists to drive. The write-side
 * authorization boundary (who's allowed to fill these fields) is Python's L5 job, checked before
 * the tiered recogniser registry runs -- not this file's concern.
 */
final class WriteOps {

    private static final Logger LOG = Logger.getLogger(WriteOps.class.getName());

    private WriteOps() {}

    /** Deferred click; the response is already sent, so a failure can only be logged. */
    static void deferClick(AbstractButton button, String name) {
        SwingUtilities.invokeLater(
                () -> {
                    try {
                        button.doClick();
                    } catch (RuntimeException e) {
                        LOG.log(Level.WARNING, "deferred click failed: " + name, e);
                    }
                });
    }

    /**
     * Resolves an optional {@code windowId} (from {@code EventBridge}'s registry) to the live
     * {@link Window} it names, or {@code null} when {@code windowId} itself is {@code null} --
     * every scoped lookup in this file goes through here first, before touching the EDT, so a
     * stale ID fails fast with a distinguishable error rather than falling back to an unscoped
     * (and possibly wrong) search. See {@code EventBridge.java} for why window IDs exist.
     *
     * <p>Package-visible: {@code Protocol.dumpResponse} reuses it for the read side.
     */
    static Window resolveScope(String windowId) {
        if (windowId == null) {
            return null;
        }
        Window window = EventBridge.resolveWindow(windowId);
        if (window == null) {
            throw new ComponentLookup.WindowGoneException(windowId);
        }
        return window;
    }

    static void setText(String target, String value, String windowId) {
        Window scope = resolveScope(windowId);
        RuntimeException[] error = new RuntimeException[1];
        ComponentLookup.runOnEdt(
                () -> {
                    Component component = ComponentLookup.findByAccessibleName(target, scope);
                    if (!(component instanceof JTextComponent textComponent)) {
                        error[0] = new ComponentLookup.ElementNotFoundException(target);
                        return;
                    }
                    textComponent.setText(value);
                });
        if (error[0] != null) {
            throw error[0];
        }
    }

    static void setCheckbox(String target, boolean checked, String windowId) {
        Window scope = resolveScope(windowId);
        RuntimeException[] error = new RuntimeException[1];
        ComponentLookup.runOnEdt(
                () -> {
                    Component component = ComponentLookup.findByAccessibleName(target, scope);
                    if (!(component instanceof AbstractButton button)) {
                        error[0] = new ComponentLookup.ElementNotFoundException(target);
                        return;
                    }
                    if (button.isSelected() != checked) {
                        button.doClick();
                    }
                });
        if (error[0] != null) {
            throw error[0];
        }
    }

    static void click(String target, String windowId) {
        Window scope = resolveScope(windowId);
        RuntimeException[] error = new RuntimeException[1];
        AbstractButton[] found = new AbstractButton[1];
        ComponentLookup.runOnEdt(
                () -> {
                    Component component = ComponentLookup.findByAccessibleName(target, scope);
                    if (!(component instanceof AbstractButton button)) {
                        error[0] = new ComponentLookup.ElementNotFoundException(target);
                        return;
                    }
                    found[0] = button;
                });
        if (error[0] != null) {
            throw error[0];
        }
        deferClick(found[0], target);
    }

    /**
     * Clicks the menu item at {@code path} ({@code "File/Exit"}). Menu items are unreachable
     * via {@link ComponentLookup#findByAccessibleName}: a {@code JMenu}'s items live in a
     * {@code JPopupMenu} outside the AWT container tree while the menu is closed.
     *
     * <p>Ported from IBC's {@code SwingUtils.findMenuItem(JMenuBar, String[])}.
     * {@code MenuElement.getSubElements()} walks the menu's structural model, which holds the
     * full menu whether or not it is open, so intermediate menus need no {@code doClick()}.
     *
     * <p>A single {@code doClick()} on the resolved leaf, as in IBC's {@code
     * Utils.invokeMenuItem}. A disabled button's {@code doClick()} is a silent no-op in Swing
     * (a blocking modal can disable the item), so this returns {@code false} when the item is
     * disabled instead of clicking. The retry loop lives in Python ({@code
     * actions.navigate_menu}), which keeps this method non-blocking.
     *
     * <p>{@code windowId}, when given, searches that window's menu bar only (as IBC scopes to
     * its main window). {@code null} uses {@link #findMenuBar()}, the first displayable
     * {@code JFrame} with a menu bar, which can pick the wrong frame when several are
     * displayable (such as TWS's login frame beside the main window).
     */
    static boolean navigateMenu(String path, String windowId) {
        Window scope = resolveScope(windowId);
        String[] parts = path.split("/");
        RuntimeException[] error = new RuntimeException[1];
        JMenuItem[] found = new JMenuItem[1];
        boolean[] enabled = new boolean[1];
        ComponentLookup.runOnEdt(
                () -> {
                    JMenuBar menuBar = scope != null ? findMenuBar(scope) : findMenuBar();
                    if (menuBar == null) {
                        error[0] = new ComponentLookup.ElementNotFoundException(path);
                        return;
                    }
                    MenuElement current = menuBar;
                    for (String part : parts) {
                        current = findMenuElement(current, part);
                        if (current == null) {
                            error[0] = new ComponentLookup.ElementNotFoundException(path);
                            return;
                        }
                    }
                    if (!(current instanceof JMenuItem item)) {
                        error[0] = new ComponentLookup.ElementNotFoundException(path);
                        return;
                    }
                    found[0] = item;
                    enabled[0] = item.isEnabled();
                });
        if (error[0] != null) {
            throw error[0];
        }
        if (!enabled[0]) {
            return false;
        }
        deferClick(found[0], path);
        return true;
    }

    /**
     * Read-only: does {@code path} resolve to a {@code JMenuItem} in {@code windowId}'s menu
     * bar, and is it enabled? Never clicks. IBC's {@code MainWindowFrameHandler} checks
     * existence only ({@code SwingUtils.findMenuItemInAnyMenuBar}); enabled is reported too
     * because a blocking modal disables the item. Unlike {@link #navigateMenu}, {@code windowId}
     * is required: the question is about one specific window.
     */
    static boolean[] menuItemExists(String path, String windowId) {
        Window scope = resolveScope(windowId);
        String[] parts = path.split("/");
        boolean[] result = new boolean[] {false, false}; // [exists, enabled]
        ComponentLookup.runOnEdt(
                () -> {
                    JMenuBar menuBar = findMenuBar(scope);
                    if (menuBar == null) {
                        return;
                    }
                    MenuElement current = menuBar;
                    for (String part : parts) {
                        current = findMenuElement(current, part);
                        if (current == null) {
                            return;
                        }
                    }
                    if (current instanceof JMenuItem item) {
                        result[0] = true;
                        result[1] = item.isEnabled();
                    }
                });
        return result;
    }

    /**
     * Ported from IBC's own {@code Utils.selectConfigSection(JDialog, String[])} --
     * walks the Global Configuration dialog's own {@code JTree} model from the root,
     * matching one path segment per level via {@code node.toString()} (IBC's own
     * {@code SwingUtils.findChildNode}, case-insensitive, matching IBC exactly), then
     * a single {@code setSelectionPath()} call -- this one call both expands the path
     * and selects the leaf, swapping the displayed settings panel, since the dialog is
     * in-process just like IBC's own. No need to {@code doClick()} through intermediate
     * tree nodes first.
     *
     * <p>{@code windowId}, when given (captured from the Configuration dialog's {@code
     * window_opened} event), resolves straight to that {@link JDialog} via {@code EventBridge}'s
     * registry, as IBC uses the {@code ConfigDialogManager} reference. When {@code null}, the
     * dialog is found by title ({@code title.contains("Configuration")}), as IBC's {@code
     * GlobalConfigurationDialogHandler.recogniseWindow} does.
     */
    static void selectConfigSection(String path, String windowId) {
        String[] parts = path.split("/");
        Window scope = resolveScope(windowId);
        RuntimeException[] error = new RuntimeException[1];
        ComponentLookup.runOnEdt(
                () -> {
                    JDialog configDialog =
                            scope instanceof JDialog dialog ? dialog : findConfigDialog();
                    if (configDialog == null) {
                        error[0] = new ComponentLookup.ElementNotFoundException(
                                "Configuration dialog");
                        return;
                    }
                    JTree tree = findTree(configDialog);
                    if (tree == null) {
                        error[0] = new ComponentLookup.ElementNotFoundException(
                                "config tree");
                        return;
                    }
                    TreeModel model = tree.getModel();
                    Object node = model.getRoot();
                    TreePath treePath = new TreePath(node);
                    for (String part : parts) {
                        Object child = findChildNode(model, node, part);
                        if (child == null) {
                            error[0] = new ComponentLookup.ElementNotFoundException(path);
                            return;
                        }
                        node = child;
                        treePath = treePath.pathByAddingChild(node);
                    }
                    tree.setExpandsSelectedPaths(true);
                    tree.setSelectionPath(treePath);
                });
        if (error[0] != null) {
            throw error[0];
        }
    }

    /** Matches IBC's {@code GlobalConfigurationDialogHandler.recogniseWindow} exactly --
     * see {@link #selectConfigSection} for why "contains", not an exact match. */
    private static JDialog findConfigDialog() {
        for (Window window : Window.getWindows()) {
            if (window instanceof JDialog dialog
                    && dialog.isDisplayable()
                    && dialog.getTitle() != null
                    && dialog.getTitle().contains("Configuration")) {
                return dialog;
            }
        }
        return null;
    }

    /** Ported from IBC's {@code SwingUtils.findTree} -- the first {@code JTree}
     * found in the container's own component hierarchy. */
    private static JTree findTree(Container container) {
        for (Component child : container.getComponents()) {
            if (child instanceof JTree tree) {
                return tree;
            }
            if (child instanceof Container childContainer) {
                JTree found = findTree(childContainer);
                if (found != null) {
                    return found;
                }
            }
        }
        return null;
    }

    /** Ported from IBC's {@code SwingUtils.findChildNode} -- case-insensitive, matching
     * IBC exactly (unlike {@code findByAccessibleName}'s exact-match convention
     * elsewhere in this agent; kept deliberately faithful to the primary source here). */
    private static Object findChildNode(TreeModel model, Object node, String text) {
        for (int i = 0; i < model.getChildCount(node); i++) {
            Object child = model.getChild(node, i);
            if (child != null && text.equalsIgnoreCase(child.toString())) {
                return child;
            }
        }
        return null;
    }

    /** First displayable {@code JFrame} with a menu bar -- only one Gateway/TWS
     * instance ever runs per JVM (Java Agent document sec 09), so there's never more
     * than one real candidate in practice. */
    private static JMenuBar findMenuBar() {
        for (Window window : Window.getWindows()) {
            if (window instanceof JFrame frame && frame.isDisplayable()) {
                JMenuBar menuBar = frame.getJMenuBar();
                if (menuBar != null) {
                    return menuBar;
                }
            }
        }
        return null;
    }

    /** Scoped variant: {@code scope}'s own menu bar only, with no fallback to the global
     * search, so a caller asking about one window never gets another's. {@code null} if
     * {@code scope} isn't a {@code JFrame} or has no menu bar (e.g. a dialog). */
    private static JMenuBar findMenuBar(Window scope) {
        return scope instanceof JFrame frame ? frame.getJMenuBar() : null;
    }

    /** One path segment's worth of traversal -- searches `container`'s own
     * sub-elements for a {@code JMenuItem}/{@code JMenu} whose text matches, recursing
     * transparently into anything else (a {@code JPopupMenu} wrapping a {@code JMenu}'s
     * real items) along the way, matching IBC's own single-segment
     * {@code findMenuItem(MenuElement, String)} exactly. */
    private static MenuElement findMenuElement(MenuElement container, String text) {
        for (MenuElement element : container.getSubElements()) {
            if (element instanceof JMenuItem item) {
                if (text.equalsIgnoreCase(item.getText())) {
                    return item;
                }
            } else {
                MenuElement found = findMenuElement(element, text);
                if (found != null) {
                    return found;
                }
            }
        }
        return null;
    }

    /**
     * Ported from IBC's own {@code ConfigureAutoLogoffOrRestartTimeTask.java} -- the "Lock and
     * Exit" time field has no accessible name, so {@link #setText} (accessible-name-based)
     * cannot target it. IBC finds it by *position* instead: locate the label above it by its own text
     * ({@code "Set Auto Log Off Time (HH:MM)"} or {@code "Set Auto Restart Time (HH:MM)"}), walk
     * up two container ancestors from that label, then take the Nth {@code JTextField} found
     * within that container (index 0 for this one -- there's only one in that panel, but the
     * index is kept general in case a future target needs a different position). Same idea as
     * IBC's own {@code SwingUtils.findLabel}/{@code getAncestorOfClass}/{@code findTextField},
     * not reinvented.
     */
    static void setTextNearLabel(
            String labelText, int textFieldIndex, String value, String windowId) {
        Window scope = resolveScope(windowId);
        RuntimeException[] error = new RuntimeException[1];
        ComponentLookup.runOnEdt(
                () -> {
                    Component label =
                            scope != null ? findLabelByTextIn(scope, labelText) : findLabelByText(labelText);
                    if (label == null) {
                        error[0] = new ComponentLookup.ElementNotFoundException(labelText);
                        return;
                    }
                    Container container = SwingUtilities.getAncestorOfClass(Container.class, label);
                    if (container != null) {
                        container =
                                SwingUtilities.getAncestorOfClass(Container.class, container);
                    }
                    if (container == null) {
                        error[0] = new ComponentLookup.ElementNotFoundException(labelText);
                        return;
                    }
                    JTextField field = findTextFieldByIndex(container, textFieldIndex);
                    if (field == null) {
                        error[0] = new ComponentLookup.ElementNotFoundException(labelText);
                        return;
                    }
                    field.setText(value);
                });
        if (error[0] != null) {
            throw error[0];
        }
    }

    private static Component findLabelByText(String text) {
        for (Window window : Window.getWindows()) {
            if (!window.isDisplayable()) {
                continue;
            }
            Component match = findLabelByTextIn(window, text);
            if (match != null) {
                return match;
            }
        }
        return null;
    }

    /** Matches on both accessible name and the label's own text -- the real case (Swing's
     * default {@code AccessibleJLabel} returns the label's text as its accessible name) and
     * a robustness fallback, same dual-check {@code NonBrokerageAccountRecognizer} already
     * uses on the Python side for the identical reason. */
    private static Component findLabelByTextIn(Component component, String text) {
        if (component instanceof JLabel label) {
            AccessibleContext context = label.getAccessibleContext();
            String accessibleName = context != null ? context.getAccessibleName() : null;
            if (text.equals(accessibleName) || text.equals(label.getText())) {
                return label;
            }
        }
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) {
                Component match = findLabelByTextIn(child, text);
                if (match != null) {
                    return match;
                }
            }
        }
        return null;
    }

    private static JTextField findTextFieldByIndex(Container container, int index) {
        List<JTextField> found = new ArrayList<>();
        collectTextFields(container, found);
        return index >= 0 && index < found.size() ? found.get(index) : null;
    }

    private static void collectTextFields(Component component, List<JTextField> out) {
        if (component instanceof JTextField field) {
            out.add(field);
        }
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) {
                collectTextFields(child, out);
            }
        }
    }
}

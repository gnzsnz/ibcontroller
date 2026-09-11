package ibcontroller.agent;

import java.awt.Component;
import java.awt.Container;
import java.awt.Window;
import java.util.ArrayList;
import java.util.List;
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

    private WriteOps() {}

    /**
     * Resolves an optional {@code windowId} (from {@code EventBridge}'s registry) to the live
     * {@link Window} it names, or {@code null} when {@code windowId} itself is {@code null} --
     * every scoped lookup in this file goes through here first, before ever touching the EDT,
     * so a stale ID fails fast with a distinguishable error rather than silently falling back to
     * an unscoped (and, per the bug this whole mechanism exists to fix, potentially wrong) search.
     * See {@code EventBridge.java}'s own docstring for why this mechanism exists at all.
     *
     * <p>Package-visible (2026-09-06), not private -- {@code Protocol.dumpResponse} reuses
     * this exact same resolution for the read side ({@code ComponentLookup.dump}'s scoped
     * overload) instead of duplicating the same two lines a second time.
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
        SwingUtilities.invokeLater(found[0]::doClick);
    }

    /**
     * Confirmed live (2026-09-05, real Gateway 10.50) that menu items are genuinely
     * unreachable via {@link ComponentLookup#findByAccessibleName} -- a {@code JMenu}'s
     * dropdown items live in a {@code JPopupMenu} that isn't part of the ordinary AWT
     * container tree {@code Window.getComponents()} walks while the menu is closed. This
     * is why {@code launcher.py}'s {@code clean_shutdown} silently fell through to its
     * hard-kill fallback instead of clicking "File &gt; Close" -- {@code click} was the
     * wrong primitive for a menu-item target, not a bug in {@code click} itself.
     *
     * <p>Ported from IBC's own {@code SwingUtils.findMenuItem(JMenuBar, String[])} --
     * {@code MenuElement.getSubElements()} walks the menu's own structural model
     * directly (not the AWT container tree), which reflects the full menu regardless of
     * whether it's currently open, since Swing menus normally have their items added at
     * construction time, not lazily on first open. No need to {@code doClick()} an
     * intermediate menu to "open" it first (confirmed by this working end to end
     * against a real Gateway; ibctl's own equivalent does open each intermediate menu,
     * possibly a defensive workaround for something version-specific -- not needed here).
     *
     * <p><b>A single {@code doClick()} on the resolved leaf, matching IBC's own
     * {@code Utils.invokeMenuItem} exactly -- confirmed still correct 2026-09-05, after a
     * real live failure of "Configure/Settings" briefly suggested otherwise.</b> The
     * apparent failure (path resolution and the click both "succeeded", but the
     * Configuration dialog never opened) was chased down two Java-side theories first --
     * clicking every intermediate segment, then doing so with a real elapsed-time gap
     * between clicks -- neither of which survived a clean re-test. The actual cause,
     * confirmed by the user's own direct manual testing: the "This is not a brokerage
     * account" warning dialog was still open at the moment Configure/Settings was
     * attempted -- Gateway won't open Global Configuration while it is (confirmed both
     * directions live: blocked while open, works immediately once closed). That's a
     * sequencing problem one layer up, but not the one originally guessed (a Python-side
     * wait with a guessed grace period before ever attempting the menu): checked IBC's
     * own {@code Utils.invokeMenuItem} (`IBC/src/ibcalpha/ibc/Utils.java`) directly and
     * found the real, grounded mechanism -- it checks {@code menuItem.isEnabled()}
     * before clicking, and retries (250ms pause) if it isn't, rather than clicking
     * unconditionally. That's the actual reason the earlier failure was silent: a
     * disabled {@code AbstractButton}'s {@code doClick()} is a no-op in Swing
     * ({@code DefaultButtonModel.setPressed()} bails out on {@code !isEnabled()}) -- no
     * exception, no action fired, so our own unconditional {@code doClick()} reported
     * success while nothing happened. Ported the same check here: this method now
     * reports whether it actually clicked, and the retry loop itself lives in Python
     * (`actions.navigate_menu`) -- matching IBC's shape (find + check + retry) but split
     * across the RPC boundary, since this method must stay non-blocking on the agent
     * side and Python already owns polling for other things (`launcher.py`'s
     * `_wait_for_ready`).
     */
    static boolean navigateMenu(String path) {
        String[] parts = path.split("/");
        RuntimeException[] error = new RuntimeException[1];
        JMenuItem[] found = new JMenuItem[1];
        boolean[] enabled = new boolean[1];
        ComponentLookup.runOnEdt(
                () -> {
                    JMenuBar menuBar = findMenuBar();
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
        SwingUtilities.invokeLater(found[0]::doClick);
        return true;
    }

    /**
     * Diagnostic, read-only, 2026-09-06 -- does {@code path} resolve to a real
     * {@code JMenuItem} in {@code windowId}'s own menu bar, and is it currently
     * enabled, without ever clicking it? Ported from IBC's own
     * {@code GatewayMainWindowFrameHandler}/{@code MainWindowFrameHandler}, whose
     * {@code recogniseWindow} checks are existence only
     * ({@code SwingUtils.findMenuItemInAnyMenuBar(window, path) != null}, no
     * {@code isEnabled()}). Reports both anyway, not just existence, per the user's own
     * live observation (2026-09-06): the non-brokerage popup blocks Configure/Settings
     * from actually opening while it's on screen, which -- since the item is already
     * *in* the tree the whole time, `main window`-or-not -- suggests IBC's
     * existence-only check might be riding on the item also being *disabled* while a
     * blocking modal owns the window, not just "does the main window exist yet." Built
     * to test a real, live-caught bug in {@code login.py}'s own main-window detection
     * (main architecture document L6/Appendix D): whether a candidate window that opens
     * <em>before</em> 2FA completes already has this item existing/enabled (meaning
     * title/class matching alone is genuinely insufficient) or gains one or both only
     * later (meaning some later signal, not captured by {@code window_opened} alone, is
     * the real one to watch for) -- not guessed, tested against a real live login, and
     * against a real 2FA/blocking-dialog window the same way. Unlike
     * {@link #navigateMenu}, {@code windowId} here is required, not optional scoping: a
     * global search would defeat the entire point of asking "does *this specific*
     * window have it yet."
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
     * <p><b>2026-09-06:</b> now takes an optional {@code windowId} -- when given (Python already
     * has it, captured from the same {@code window_opened} event that told it the Configuration
     * dialog appeared), this resolves straight to that exact {@link JDialog} via {@code
     * EventBridge}'s registry, matching IBC's own direct-object-reference approach for real. When
     * {@code null} (an older, not-yet-migrated caller), falls back to the original title-based
     * search below -- IBC gets its own direct {@code JDialog} reference from {@code ConfigDialogManager},
     * populated by its window-open handler; before this, our agent had no equivalent, so the
     * config dialog was found fresh on each call by title instead, matching IBC's own
     * {@code GlobalConfigurationDialogHandler.recogniseWindow} logic ({@code
     * title.contains("Configuration")}) rather than its actual object-reference mechanism.
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

    /** Scoped variant (2026-09-06, diagnostic use) -- {@code scope}'s own menu bar only,
     * no fallback to the global search, since precision is the entire point of passing a
     * specific window in the first place (see {@link #menuItemExists}). {@code null} if
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
     * Exit" time field genuinely has no accessible name at all (confirmed live, 2026-09-06,
     * against a real Gateway 10.50 dump: {@code accessible_name=null} on the one
     * {@code JTextField} in that panel), so {@link #setText} (accessible-name-based) can never
     * target it. IBC finds it by *position* instead: locate the label above it by its own text
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

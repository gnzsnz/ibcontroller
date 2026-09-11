package ibcontroller.agent;

import java.awt.Component;
import javax.swing.JPasswordField;

/**
 * Credential-field boundary (Java Agent document sec 05, sec 08): {@code get_text} refuses
 * outright on any password-type field, and {@code dump}'s tree output masks password contents
 * to {@code "<redacted password len=N>"}.
 *
 * <p>Provisional: {@code instanceof JPasswordField} catches the standard case, but the
 * architecture document's Appendix D already flags that Gateway uses custom, non-stock widget
 * classes elsewhere in this exact login window -- worth re-checking this specific class against
 * a real {@code dump} of the live login window before trusting it, not assumed correct here.
 */
final class Security {

    private Security() {}

    static boolean isCredentialField(Component component) {
        return component instanceof JPasswordField;
    }

    static String redactedPlaceholder(int length) {
        return "<redacted password len=" + length + ">";
    }
}

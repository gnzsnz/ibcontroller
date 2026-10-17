package ibcontroller.agent;

import java.awt.Component;
import javax.swing.JPasswordField;

/**
 * Credential-field boundary (Java Agent document sec 05, sec 08): {@code get_text} refuses
 * outright on any password-type field, and {@code dump}'s tree output masks password contents
 * to {@code "<redacted password len=N>"}.
 *
 * <p>{@code instanceof JPasswordField} is the check, not a class-name match: Gateway's password
 * field is the custom {@code twslaunch.trader.common.document.q}, a {@code JPasswordField}
 * subclass; the Gateway and TWS login frames use the same class.
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

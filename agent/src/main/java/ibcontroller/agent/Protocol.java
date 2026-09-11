package ibcontroller.agent;

import java.awt.Window;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.channels.Channels;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Command-socket accept loop and JSON Lines dispatch (Java Agent document sec 06-07).
 *
 * <p>Current scope (Phase 1 step 2): {@code ping} only. Every other command in sec 07 lands
 * with the step that implements it (ComponentLookup, WriteOps, EventBridge).
 */
final class Protocol {

    private static final Logger LOG = Logger.getLogger(Protocol.class.getName());

    private static final Instant STARTED = Instant.now();
    private static final String VERSION = "0.0.1-dev";

    private Protocol() {}

    static void handleCommandClient(SocketChannel client) {
        try (BufferedReader in =
                        new BufferedReader(Channels.newReader(client, StandardCharsets.UTF_8));
                PrintWriter out =
                        new PrintWriter(Channels.newWriter(client, StandardCharsets.UTF_8), true)) {
            LOG.fine("command client connected");
            String line;
            while ((line = in.readLine()) != null) {
                if (line.isBlank()) {
                    continue;
                }
                Map<String, Object> response = dispatch(line);
                out.println(Json.write(response));
                Object error = response.get("error");
                if (error == null) {
                    LOG.fine("cmd ok: " + summarize(line));
                } else {
                    LOG.warning("cmd error " + error + ": " + summarize(line));
                }
            }
        } catch (IOException e) {
            LOG.log(Level.WARNING, "command client error: " + e.getMessage(), e);
        }
    }

    /** Best-effort summary of a request line for the log -- omits values entirely (a
     * {@code set_text} value may be a credential; logging it on the Java side would bypass
     * Python's {@code Secret} redaction), keeps the command name and the target/label/path
     * it operates on. Never logs values, never logs window titles or component text. */
    private static String summarize(String line) {
        String cmd = extractField(line, "cmd");
        String target = extractField(line, "target");
        if (target != null) {
            return cmd + " target=" + target;
        }
        String label = extractField(line, "label");
        if (label != null) {
            return cmd + " label=" + label;
        }
        String path = extractField(line, "path");
        if (path != null) {
            return cmd + " path=" + path;
        }
        String window = extractField(line, "window_id");
        if (window != null) {
            return cmd + " window_id=" + window;
        }
        return cmd != null ? cmd : "(malformed)";
    }

    /** Cheap, string-only extraction of one top-level field's value from a JSON line, for the
     * log summary only -- intentionally not parsing the whole line (that's {@code Json.parse}'s
     * real job, already done before this runs). */
    private static String extractField(String line, String field) {
        String key = "\"" + field + "\"";
        int idx = line.indexOf(key);
        if (idx < 0) {
            return null;
        }
        int colon = line.indexOf(':', idx + key.length());
        if (colon < 0) {
            return null;
        }
        int start = colon + 1;
        while (start < line.length() && Character.isWhitespace(line.charAt(start))) {
            start++;
        }
        if (start >= line.length()) {
            return null;
        }
        char first = line.charAt(start);
        if (first == '"') {
            int end = start + 1;
            while (end < line.length() && line.charAt(end) != '"') {
                end++;
            }
            return end >= line.length() ? null : line.substring(start + 1, end);
        }
        if (first == '{') {
            return null;
        }
        int end = start;
        while (end < line.length() && line.charAt(end) != ',') {
            end++;
        }
        return line.substring(start, end);
    }

    private static Map<String, Object> dispatch(String line) {
        Map<String, Object> request;
        try {
            request = asObject(Json.parse(line));
        } catch (RuntimeException e) {
            return errorResponse("bad_request", e.getMessage());
        }
        Object cmd = request.get("cmd");
        if ("ping".equals(cmd)) {
            return pingResponse();
        }
        if ("dump".equals(cmd)) {
            return dumpResponse(request);
        }
        if ("get_text".equals(cmd)) {
            return getTextResponse(request);
        }
        if ("set_text".equals(cmd)) {
            return setTextResponse(request);
        }
        if ("set_checkbox".equals(cmd)) {
            return setCheckboxResponse(request);
        }
        if ("click".equals(cmd)) {
            return clickResponse(request);
        }
        if ("navigate_menu".equals(cmd)) {
            return navigateMenuResponse(request);
        }
        if ("expand_tree".equals(cmd)) {
            return expandTreeResponse(request);
        }
        if ("set_text_near_label".equals(cmd)) {
            return setTextNearLabelResponse(request);
        }
        if ("menu_item_exists".equals(cmd)) {
            return menuItemExistsResponse(request);
        }
        return errorResponse("unknown_command", String.valueOf(cmd));
    }

    /** {@code window_id} is optional on every command below that supports scoping -- absent
     * means "search globally", matching each write op's own backward-compatible default
     * (EventBridge.java's/WriteOps.java's own docstrings have the full mechanism). */
    private static String windowId(Map<String, Object> request) {
        Object value = request.get("window_id");
        return value instanceof String s ? s : null;
    }

    private static Map<String, Object> setTextResponse(Map<String, Object> request) {
        Object target = request.get("target");
        Object value = request.get("value");
        if (!(target instanceof String targetName) || !(value instanceof String valueString)) {
            return errorResponse("bad_request", "target and value must both be strings");
        }
        try {
            WriteOps.setText(targetName, valueString, windowId(request));
            return okResponse();
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> setCheckboxResponse(Map<String, Object> request) {
        Object target = request.get("target");
        Object checked = request.get("checked");
        if (!(target instanceof String targetName) || !(checked instanceof Boolean checkedValue)) {
            return errorResponse("bad_request", "target must be a string, checked a boolean");
        }
        try {
            WriteOps.setCheckbox(targetName, checkedValue, windowId(request));
            return okResponse();
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> clickResponse(Map<String, Object> request) {
        Object target = request.get("target");
        if (!(target instanceof String targetName)) {
            return errorResponse("bad_request", "target must be a string");
        }
        try {
            WriteOps.click(targetName, windowId(request));
            return okResponse();
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> navigateMenuResponse(Map<String, Object> request) {
        Object path = request.get("path");
        if (!(path instanceof String pathString)) {
            return errorResponse("bad_request", "path must be a string");
        }
        try {
            boolean clicked = WriteOps.navigateMenu(pathString);
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("ok", true);
            response.put("clicked", clicked);
            return response;
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> expandTreeResponse(Map<String, Object> request) {
        Object path = request.get("path");
        if (!(path instanceof String pathString)) {
            return errorResponse("bad_request", "path must be a string");
        }
        try {
            WriteOps.selectConfigSection(pathString, windowId(request));
            return okResponse();
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> setTextNearLabelResponse(Map<String, Object> request) {
        Object label = request.get("label");
        Object index = request.get("index");
        Object value = request.get("value");
        if (!(label instanceof String labelText)
                || !(index instanceof Number indexNumber)
                || !(value instanceof String valueString)) {
            return errorResponse(
                    "bad_request", "label and value must be strings, index a number");
        }
        try {
            WriteOps.setTextNearLabel(
                    labelText, indexNumber.intValue(), valueString, windowId(request));
            return okResponse();
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    /** Diagnostic-only (2026-09-06), see {@link WriteOps#menuItemExists} -- {@code window_id}
     * is required here, not optional scoping like everything else in this file, since the
     * whole point is asking about one specific window. */
    private static Map<String, Object> menuItemExistsResponse(Map<String, Object> request) {
        Object path = request.get("path");
        String windowId = windowId(request);
        if (!(path instanceof String pathString)) {
            return errorResponse("bad_request", "path must be a string");
        }
        if (windowId == null) {
            return errorResponse("bad_request", "window_id is required");
        }
        try {
            boolean[] result = WriteOps.menuItemExists(pathString, windowId);
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("ok", true);
            response.put("exists", result[0]);
            response.put("enabled", result[1]);
            return response;
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> okResponse() {
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", true);
        return response;
    }

    private static Map<String, Object> dumpResponse(Map<String, Object> request) {
        Object window = request.get("window");
        String titleFilter = window instanceof String s ? s : null;
        try {
            Window scope = WriteOps.resolveScope(windowId(request));
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("ok", true);
            response.put("components", ComponentLookup.dump(titleFilter, scope));
            return response;
        } catch (ComponentLookup.WindowGoneException e) {
            return errorResponse("window_gone", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> getTextResponse(Map<String, Object> request) {
        Object target = request.get("target");
        if (!(target instanceof String targetName)) {
            return errorResponse("bad_request", "target must be a string");
        }
        try {
            String value = ComponentLookup.getText(targetName);
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("ok", true);
            response.put("value", value);
            return response;
        } catch (ComponentLookup.CredentialRefusedException e) {
            return errorResponse("refused_credential_field", e.getMessage());
        } catch (ComponentLookup.ElementNotFoundException e) {
            return errorResponse("not_found", e.getMessage());
        } catch (RuntimeException e) {
            return errorResponse("agent_error", e.getMessage());
        }
    }

    private static Map<String, Object> pingResponse() {
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", true);
        response.put("version", VERSION);
        response.put("uptime_s", Duration.between(STARTED, Instant.now()).toSeconds());
        return response;
    }

    private static Map<String, Object> errorResponse(String code, String detail) {
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", false);
        response.put("error", code);
        response.put("detail", detail);
        return response;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asObject(Object value) {
        if (value instanceof Map) {
            return (Map<String, Object>) value;
        }
        throw new IllegalArgumentException("expected a JSON object");
    }

    /**
     * Hand-rolled JSON, not a library (Java Agent document sec 06, decided) -- every message
     * this agent sends or receives is one of the shapes sec 07 enumerates: flat objects,
     * strings, numbers, booleans, null, with at most modest nesting (an {@code event}'s
     * {@code window} field, {@code dump}'s {@code components} array). A general-purpose parser
     * would handle documents this agent will never be sent; this one only handles those shapes.
     */
    static final class Json {

        private Json() {}

        static Object parse(String text) {
            Parser parser = new Parser(text);
            parser.skipWhitespace();
            Object value = parser.parseValue();
            parser.skipWhitespace();
            if (!parser.atEnd()) {
                throw new IllegalArgumentException("trailing content after JSON value");
            }
            return value;
        }

        static String write(Object value) {
            StringBuilder out = new StringBuilder();
            writeValue(value, out);
            return out.toString();
        }

        // Plain instanceof, not switch pattern matching (Java 21+) -- this needs to run on
        // whatever JRE Gateway/TWS actually bundles, confirmed as low as Java 17 (Gateway
        // 10.50). instanceof pattern variables (Java 16+) are fine; switch patterns aren't.
        private static void writeValue(Object value, StringBuilder out) {
            if (value == null) {
                out.append("null");
            } else if (value instanceof String s) {
                writeString(s, out);
            } else if (value instanceof Boolean b) {
                out.append(b);
            } else if (value instanceof Map<?, ?> map) {
                writeObject(map, out);
            } else if (value instanceof List<?> list) {
                writeArray(list, out);
            } else if (value instanceof Double d) {
                out.append(d);
            } else if (value instanceof Number n) {
                out.append(n);
            } else {
                throw new IllegalArgumentException(
                        "unsupported JSON value type: " + value.getClass());
            }
        }

        private static void writeObject(Map<?, ?> map, StringBuilder out) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                writeString(String.valueOf(entry.getKey()), out);
                out.append(':');
                writeValue(entry.getValue(), out);
            }
            out.append('}');
        }

        private static void writeArray(List<?> list, StringBuilder out) {
            out.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                writeValue(item, out);
            }
            out.append(']');
        }

        private static void writeString(String s, StringBuilder out) {
            out.append('"');
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"' -> out.append("\\\"");
                    case '\\' -> out.append("\\\\");
                    case '\n' -> out.append("\\n");
                    case '\r' -> out.append("\\r");
                    case '\t' -> out.append("\\t");
                    default -> {
                        if (c < 0x20) {
                            out.append(String.format("\\u%04x", (int) c));
                        } else {
                            out.append(c);
                        }
                    }
                }
            }
            out.append('"');
        }

        /** Cursor-based recursive-descent parser over a single, already-complete JSON text. */
        private static final class Parser {
            private final String text;
            private int pos;

            Parser(String text) {
                this.text = text;
            }

            boolean atEnd() {
                return pos >= text.length();
            }

            void skipWhitespace() {
                while (pos < text.length() && Character.isWhitespace(text.charAt(pos))) {
                    pos++;
                }
            }

            Object parseValue() {
                skipWhitespace();
                if (atEnd()) {
                    throw new IllegalArgumentException("unexpected end of JSON input");
                }
                char c = text.charAt(pos);
                return switch (c) {
                    case '{' -> parseObject();
                    case '[' -> parseArray();
                    case '"' -> parseString();
                    case 't', 'f' -> parseBoolean();
                    case 'n' -> parseNull();
                    default -> parseNumber();
                };
            }

            private Map<String, Object> parseObject() {
                expect('{');
                Map<String, Object> result = new LinkedHashMap<>();
                skipWhitespace();
                if (peek() == '}') {
                    pos++;
                    return result;
                }
                while (true) {
                    skipWhitespace();
                    String key = parseString();
                    skipWhitespace();
                    expect(':');
                    Object value = parseValue();
                    result.put(key, value);
                    skipWhitespace();
                    char next = expectOneOf(',', '}');
                    if (next == '}') {
                        return result;
                    }
                }
            }

            private List<Object> parseArray() {
                expect('[');
                List<Object> result = new ArrayList<>();
                skipWhitespace();
                if (peek() == ']') {
                    pos++;
                    return result;
                }
                while (true) {
                    result.add(parseValue());
                    skipWhitespace();
                    char next = expectOneOf(',', ']');
                    if (next == ']') {
                        return result;
                    }
                }
            }

            private String parseString() {
                expect('"');
                StringBuilder out = new StringBuilder();
                while (true) {
                    if (atEnd()) {
                        throw new IllegalArgumentException("unterminated JSON string");
                    }
                    char c = text.charAt(pos++);
                    if (c == '"') {
                        return out.toString();
                    }
                    if (c != '\\') {
                        out.append(c);
                        continue;
                    }
                    char escaped = text.charAt(pos++);
                    switch (escaped) {
                        case '"' -> out.append('"');
                        case '\\' -> out.append('\\');
                        case '/' -> out.append('/');
                        case 'b' -> out.append('\b');
                        case 'f' -> out.append('\f');
                        case 'n' -> out.append('\n');
                        case 'r' -> out.append('\r');
                        case 't' -> out.append('\t');
                        case 'u' -> {
                            out.append((char) Integer.parseInt(text.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default ->
                                throw new IllegalArgumentException(
                                        "invalid JSON escape: \\" + escaped);
                    }
                }
            }

            private Boolean parseBoolean() {
                if (text.startsWith("true", pos)) {
                    pos += 4;
                    return Boolean.TRUE;
                }
                if (text.startsWith("false", pos)) {
                    pos += 5;
                    return Boolean.FALSE;
                }
                throw new IllegalArgumentException("invalid JSON literal at position " + pos);
            }

            private Object parseNull() {
                if (text.startsWith("null", pos)) {
                    pos += 4;
                    return null;
                }
                throw new IllegalArgumentException("invalid JSON literal at position " + pos);
            }

            private Number parseNumber() {
                int start = pos;
                if (peek() == '-') {
                    pos++;
                }
                while (!atEnd() && Character.isDigit(text.charAt(pos))) {
                    pos++;
                }
                boolean isDouble = false;
                if (!atEnd() && text.charAt(pos) == '.') {
                    isDouble = true;
                    pos++;
                    while (!atEnd() && Character.isDigit(text.charAt(pos))) {
                        pos++;
                    }
                }
                if (!atEnd() && (text.charAt(pos) == 'e' || text.charAt(pos) == 'E')) {
                    isDouble = true;
                    pos++;
                    if (!atEnd() && (text.charAt(pos) == '+' || text.charAt(pos) == '-')) {
                        pos++;
                    }
                    while (!atEnd() && Character.isDigit(text.charAt(pos))) {
                        pos++;
                    }
                }
                String token = text.substring(start, pos);
                if (token.isEmpty() || "-".equals(token)) {
                    throw new IllegalArgumentException("invalid JSON number at position " + start);
                }
                return isDouble ? (Number) Double.parseDouble(token) : (Number) Long.parseLong(token);
            }

            private char peek() {
                if (atEnd()) {
                    throw new IllegalArgumentException("unexpected end of JSON input");
                }
                return text.charAt(pos);
            }

            private void expect(char expected) {
                if (peek() != expected) {
                    throw new IllegalArgumentException(
                            "expected '" + expected + "' at position " + pos);
                }
                pos++;
            }

            private char expectOneOf(char a, char b) {
                char c = peek();
                if (c != a && c != b) {
                    throw new IllegalArgumentException(
                            "expected '" + a + "' or '" + b + "' at position " + pos);
                }
                pos++;
                return c;
            }
        }
    }
}

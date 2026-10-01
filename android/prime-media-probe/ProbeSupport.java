package dev.tvprobe.mediasession;

import org.json.JSONArray;
import org.json.JSONObject;
import java.util.LinkedHashMap;
import java.util.Map;

/** Android-independent pieces shared by the service and fault tests. */
final class ProbeSupport {
    static final int SCHEMA = 2;
    static final String BUILD = "2.0.0";
    interface Clock { long wall(); long elapsed(); }
    static final class Stamp {
        final long wall, elapsed;
        Stamp(Clock clock) { elapsed = clock.elapsed(); wall = clock.wall(); }
        JSONObject json() { return obj("wallTimeMs", wall, "elapsedRealtimeMs", elapsed); }
    }
    static JSONObject obj(Object... pairs) {
        JSONObject o = new JSONObject();
        for (int i=0; i<pairs.length; i+=2) put(o, (String)pairs[i], pairs[i+1]);
        return o;
    }
    static void put(JSONObject o, String key, Object value) {
        try { o.put(key, value == null ? JSONObject.NULL : value); }
        catch (Exception e) { throw new IllegalArgumentException("Invalid JSON field: " + key, e); }
    }
    static JSONObject copy(JSONObject value) {
        if (value == null) return null;
        try { return new JSONObject(value.toString()); }
        catch (Exception e) { throw new IllegalArgumentException(e); }
    }
    static JSONObject error(Throwable e) {
        String text = e.getMessage();
        return obj("errorType", e.getClass().getName(), "message",
            text == null ? null : text.substring(0, Math.min(text.length(), 512)));
    }
    static final class Operation {
        long attempts, successes, failures;
        Stamp lastAttempt, lastSuccess, lastFailure;
        JSONObject lastError;
        boolean lastAttemptSucceeded, inProgress;
        void attempt(Clock c) { attempts++; lastAttempt = new Stamp(c); lastAttemptSucceeded = false; inProgress = true; }
        void success(Clock c) { successes++; lastSuccess = new Stamp(c); lastAttemptSucceeded = true; inProgress = false; }
        void fail(Clock c, Throwable e) { failures++; lastFailure = new Stamp(c); lastError = error(e); lastAttemptSucceeded = false; inProgress = false; }
        JSONObject json() {
            return obj("attempts", attempts, "successes", successes, "failures", failures,
                "lastAttempt", json(lastAttempt), "lastSuccess", json(lastSuccess),
                "lastFailure", json(lastFailure), "lastError", lastError,
                "lastAttemptSucceeded", lastAttemptSucceeded, "inProgress", inProgress);
        }
        static Object json(Stamp t) { return t == null ? null : t.json(); }
    }
    static final class Lifetime {
        final String id;
        JSONObject lastKnown;
        boolean active = true;
        Lifetime(String id) { this.id = id; }
        void remember(JSONObject snapshot) { lastKnown = copy(snapshot); }
        JSONObject finish(String reason, Stamp at) {
            if (!active) return null;
            active = false;
            return obj("sessionInstanceId", id, "removalReason", reason,
                "removalObservedAt", at.json(), "historical", true,
                "eventCompletionInferred", false, "lastKnownSnapshot", copy(lastKnown));
        }
    }
    static final class Lifetimes<K> {
        final Map<K, Lifetime> active = new LinkedHashMap<>();
        long counter;
        Lifetime add(K token) {
            Lifetime v = active.get(token);
            if (v == null) { v = new Lifetime("s" + (++counter)); active.put(token, v); }
            return v;
        }
        JSONObject remove(K token, String reason, Stamp at) {
            Lifetime v = active.remove(token);
            return v == null ? null : v.finish(reason, at);
        }
    }
    /** A shared construction budget, in addition to the final encoded-byte ceiling. */
    static final class Budget {
        static final int MAX_ITEMS = 64, MAX_STRING = 4096, MAX_DEPTH = 8;
        int nodes = 2048, chars = 32768, omitted;
        final JSONArray reasons = new JSONArray();
        boolean take(int depth) {
            if (depth > MAX_DEPTH || nodes-- <= 0) { mark("depth_or_node_budget"); return false; }
            return true;
        }
        boolean exhausted() { return nodes <= 0 || chars <= 0; }
        void mark(String reason) { omitted++; if (reasons.length() < 32) reasons.put(reason); }
        Object text(CharSequence input) {
            if (input == null) return JSONObject.NULL;
            int length = input.length(), n = Math.min(length, Math.min(MAX_STRING, Math.max(0, chars)));
            // Do not split a UTF-16 surrogate pair.
            if (n > 0 && n < length && Character.isHighSurrogate(input.charAt(n-1))) n--;
            chars -= n;
            String prefix = input.subSequence(0, n).toString();
            if (n == length) return prefix;
            mark("string_limit");
            return obj("type", "string", "truncated", true, "originalLength", length, "prefix", prefix);
        }
        JSONObject marker(String reason) { mark(reason); return obj("omitted", true, "reason", reason); }
        JSONObject json() { return obj("complete", omitted == 0, "omissions", omitted, "reasons", reasons); }
    }
}

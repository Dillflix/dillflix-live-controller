package dev.tvprobe.mediasession;

import android.content.ComponentName;
import android.graphics.Bitmap;
import android.media.MediaDescription;
import android.media.MediaMetadata;
import android.media.Rating;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.Bundle;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.Parcel;
import android.os.SystemClock;
import android.service.notification.NotificationListenerService;
import android.util.Base64;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintWriter;
import java.lang.reflect.Array;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/** Read-only observer. No playback commands, notification content handling, or network access. */
public class ProbeService extends NotificationListenerService {
    private static final String TARGET = "com.amazon.firebat";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Map<MediaSession.Token, Watch> watches = new HashMap<>();
    private MediaSessionManager manager;
    private volatile boolean connected;
    private long sequence;
    private String lastPoll;
    private final MediaSessionManager.OnActiveSessionsChangedListener listener = new MediaSessionManager.OnActiveSessionsChangedListener() {
        @Override public void onActiveSessionsChanged(List<MediaController> sessions) {
            refresh(sessions);
            emit("sessions_changed", null);
        }
    };
    private final Runnable poll = new Runnable() {
        public void run() {
            if (!connected) return;
            try { refresh(sessions()); emit("poll", null); }
            catch (Exception e) { emit("poll_error", error(e)); }
            handler.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        manager = (MediaSessionManager) getSystemService(MEDIA_SESSION_SERVICE);
    }
    private ComponentName component() { return new ComponentName(this, ProbeService.class); }
    private List<MediaController> sessions() { return manager.getActiveSessions(component()); }
    @Override public void onListenerConnected() {
        stopWatching();
        connected = true;
        try {
            manager.addOnActiveSessionsChangedListener(listener, component(), handler);
            refresh(sessions());
            emit("connected", null);
            handler.post(poll);
        } catch (Exception e) { emit("connect_error", error(e)); }
    }
    @Override public void onListenerDisconnected() {
        stopWatching();
        emit("disconnected", null);
    }
    @Override public void onDestroy() { stopWatching(); super.onDestroy(); }
    private void stopWatching() {
        connected = false;
        handler.removeCallbacks(poll);
        if (manager != null) {
            try { manager.removeOnActiveSessionsChangedListener(listener); }
            catch (RuntimeException ignored) { }
        }
        for (Watch w : watches.values()) w.controller.unregisterCallback(w.callback);
        watches.clear();
        lastPoll = null;
    }
    private void refresh(List<MediaController> sessions) {
        Map<MediaSession.Token, MediaController> found = new HashMap<>();
        if (sessions != null) for (MediaController c : sessions) {
            if (TARGET.equals(c.getPackageName())) found.put(c.getSessionToken(), c);
        }
        for (MediaSession.Token t : new ArrayList<>(watches.keySet())) {
            if (!found.containsKey(t)) {
                Watch w = watches.remove(t);
                w.controller.unregisterCallback(w.callback);
            }
        }
        for (Map.Entry<MediaSession.Token, MediaController> e : found.entrySet()) {
            if (!watches.containsKey(e.getKey())) watches.put(e.getKey(), new Watch(e.getValue()));
        }
    }
    private class Watch {
        final MediaController controller;
        final MediaController.Callback callback;
        Watch(MediaController c) {
            controller = c;
            callback = new MediaController.Callback() {
                private void event(String name, Object payload) {
                    emit(name, obj("sessionToken", token(controller), "value", payload));
                }
                @Override public void onMetadataChanged(MediaMetadata m) { event("metadata_changed", metadata(m)); }
                @Override public void onPlaybackStateChanged(PlaybackState s) { event("playback_state_changed", state(s)); }
                @Override public void onExtrasChanged(Bundle b) { event("extras_changed", value(b, 0)); }
                @Override public void onQueueChanged(List<MediaSession.QueueItem> q) { event("queue_changed", queue(q)); }
                @Override public void onQueueTitleChanged(CharSequence t) { event("queue_title_changed", value(t, 0)); }
                @Override public void onSessionEvent(String name, Bundle b) {
                    event("session_event", obj("name", name, "extras", value(b, 0)));
                }
                @Override public void onSessionDestroyed() { event("session_destroyed", null); }
                @Override public void onAudioInfoChanged(MediaController.PlaybackInfo i) { event("audio_info_changed", info(i)); }
            };
            c.registerCallback(callback, handler);
        }
    }

    private JSONObject snapshot() {
        JSONArray result = new JSONArray();
        try {
            for (MediaController c : sessions()) {
                if (!TARGET.equals(c.getPackageName())) continue;
                try {
                    result.put(obj("package", c.getPackageName(), "sessionToken", token(c),
                        "metadata", metadata(c.getMetadata()), "sessionExtras", value(c.getExtras(), 0),
                        "playbackState", state(c.getPlaybackState()), "queueTitle", value(c.getQueueTitle(), 0),
                        "queue", queue(c.getQueue()), "flags", c.getFlags(), "ratingType", c.getRatingType(),
                        "playbackInfo", info(c.getPlaybackInfo())));
                } catch (Exception e) { result.put(obj("package", c.getPackageName(), "readError", error(e))); }
            }
            return obj("listenerConnected", connected, "sessions", result);
        } catch (Exception e) { return obj("listenerConnected", connected, "error", error(e)); }
    }
    private static String token(MediaController c) {
        // Process-local correlation marker only; never a content ID.
        return Integer.toHexString(c.getSessionToken().hashCode());
    }
    private static Object metadata(MediaMetadata m) {
        if (m == null) return JSONObject.NULL;
        Parcel p = Parcel.obtain();
        try {
            // AOSP's MediaMetadata parcel starts with its Bundle. No hidden-API reflection.
            // Report failures explicitly if a vendor changes this representation.
            m.writeToParcel(p, 0);
            p.setDataPosition(0);
            Bundle b = p.readBundle(ProbeService.class.getClassLoader());
            Object entries = value(b, 0);
            JSONArray keys = new JSONArray();
            for (String k : new TreeSet<>(m.keySet())) keys.put(k);
            return obj("keys", keys, "entries", entries, "description", description(m.getDescription()));
        } catch (Exception e) { return obj("decodeError", error(e), "descriptionText", String.valueOf(m.getDescription())); }
        finally { p.recycle(); }
    }
    private static Object state(PlaybackState s) {
        if (s == null) return JSONObject.NULL;
        JSONArray custom = new JSONArray();
        for (PlaybackState.CustomAction a : s.getCustomActions()) {
            custom.put(obj("action", a.getAction(), "name", value(a.getName(), 0), "icon", a.getIcon(), "extras", value(a.getExtras(), 0)));
        }
        String[] names = {"NONE", "STOPPED", "PAUSED", "PLAYING", "FAST_FORWARDING", "REWINDING", "BUFFERING", "ERROR", "CONNECTING", "SKIPPING_TO_PREVIOUS", "SKIPPING_TO_NEXT", "SKIPPING_TO_QUEUE_ITEM"};
        int n = s.getState();
        return obj("state", n, "stateName", n >= 0 && n < names.length ? names[n] : "UNKNOWN",
            "positionMs", s.getPosition(), "bufferedPositionMs", s.getBufferedPosition(), "speed", s.getPlaybackSpeed(),
            "updatedElapsedRealtimeMs", s.getLastPositionUpdateTime(), "actions", s.getActions(),
            "activeQueueItemId", s.getActiveQueueItemId(), "errorMessage", value(s.getErrorMessage(), 0),
            "extras", value(s.getExtras(), 0), "customActions", custom);
    }
    private static Object description(MediaDescription d) {
        if (d == null) return JSONObject.NULL;
        return obj("mediaId", d.getMediaId(), "title", value(d.getTitle(), 0), "subtitle", value(d.getSubtitle(), 0),
            "description", value(d.getDescription(), 0), "iconUri", value(d.getIconUri(), 0),
            "iconBitmap", value(d.getIconBitmap(), 0), "mediaUri", Build.VERSION.SDK_INT >= 23 ? value(d.getMediaUri(), 0) : JSONObject.NULL,
            "extras", value(d.getExtras(), 0));
    }
    private static Object queue(List<MediaSession.QueueItem> q) {
        if (q == null) return JSONObject.NULL;
        JSONArray a = new JSONArray();
        for (MediaSession.QueueItem i : q) a.put(obj("queueId", i.getQueueId(), "description", description(i.getDescription())));
        return a;
    }
    private static Object info(MediaController.PlaybackInfo i) {
        if (i == null) return JSONObject.NULL;
        return obj("playbackType", i.getPlaybackType(), "volumeControl", i.getVolumeControl(), "currentVolume", i.getCurrentVolume(),
            "maxVolume", i.getMaxVolume(), "audioAttributes", String.valueOf(i.getAudioAttributes()));
    }
    private static Object value(Object v, int depth) {
        if (v == null) return JSONObject.NULL;
        if (depth > 16) return obj("type", v.getClass().getName(), "omitted", "nesting exceeds 16");
        if (v instanceof CharSequence) return v.toString();
        if (v instanceof Boolean || v instanceof Number) return v;
        if (v instanceof Bundle) {
            JSONObject out = new JSONObject();
            try {
                Bundle b = (Bundle) v;
                for (String key : new TreeSet<>(b.keySet())) {
                    try { put(out, key, value(b.get(key), depth + 1)); }
                    catch (Exception e) { put(out, key, error(e)); }
                }
            } catch (Exception e) { put(out, "_bundleReadError", error(e)); }
            return out;
        }
        if (v instanceof Bitmap) {
            Bitmap b = (Bitmap) v;
            return obj("type", "android.graphics.Bitmap", "width", b.getWidth(), "height", b.getHeight(), "pixelsOmitted", true);
        }
        if (v instanceof Rating) {
            Rating r = (Rating) v;
            return obj("type", "android.media.Rating", "style", r.getRatingStyle(), "rated", r.isRated(),
                "heart", r.hasHeart(), "thumbUp", r.isThumbUp(), "stars", r.getStarRating(), "percent", r.getPercentRating());
        }
        if (v instanceof byte[]) return obj("type", "byte[]", "base64", Base64.encodeToString((byte[]) v, Base64.NO_WRAP));
        if (v instanceof Iterable) {
            JSONArray a = new JSONArray(); for (Object item : (Iterable<?>) v) a.put(value(item, depth + 1)); return a;
        }
        if (v.getClass().isArray()) {
            JSONArray a = new JSONArray(); for (int n = 0; n < Array.getLength(v); n++) a.put(value(Array.get(v, n), depth + 1)); return a;
        }
        return obj("type", v.getClass().getName(), "text", String.valueOf(v));
    }
    private static JSONObject error(Exception e) { return obj("errorType", e.getClass().getName(), "message", e.getMessage()); }
    private static void put(JSONObject o, String k, Object v) {
        try { o.put(k, v == null ? JSONObject.NULL : v); }
        catch (Exception e) { try { o.put(k, String.valueOf(v)); } catch (Exception ignored) { } }
    }
    private static JSONObject obj(Object... pairs) {
        JSONObject o = new JSONObject();
        for (int i = 0; i < pairs.length; i += 2) put(o, (String) pairs[i], pairs[i + 1]);
        return o;
    }
    private synchronized void emit(String reason, Object payload) {
        JSONObject current = snapshot();
        String comparison = current.toString();
        if ("poll".equals(reason) && comparison.equals(lastPoll)) return;
        lastPoll = comparison;
        JSONObject entry = obj("sequence", ++sequence, "wallTimeMs", System.currentTimeMillis(),
            "elapsedRealtimeMs", SystemClock.elapsedRealtime(), "reason", reason,
            "eventPayload", payload, "snapshot", current, "snapshotAtomic", false);
        File file = new File(getFilesDir(), "events.jsonl");
        try {
            if (file.length() > 8 * 1024 * 1024) {
                File old = new File(getFilesDir(), "events.previous.jsonl");
                if (old.exists() && !old.delete()) throw new java.io.IOException("Cannot remove old trace");
                if (!file.renameTo(old)) throw new java.io.IOException("Cannot rotate trace");
            }
            try (FileOutputStream out = new FileOutputStream(file, true)) {
                out.write((entry.toString() + "\n").getBytes(StandardCharsets.UTF_8));
            }
            Log.i("PVProbe", "sequence=" + sequence + " reason=" + reason + " (full JSON in files/events.jsonl)");
        } catch (Exception e) { Log.e("PVProbe", "Cannot write trace", e); }
    }
    @Override protected void dump(FileDescriptor fd, PrintWriter writer, String[] args) {
        writer.println(obj("wallTimeMs", System.currentTimeMillis(), "elapsedRealtimeMs", SystemClock.elapsedRealtime(),
            "snapshotAtomic", false, "snapshot", snapshot()).toString());
    }
}

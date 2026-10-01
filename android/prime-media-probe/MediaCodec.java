package dev.tvprobe.mediasession;

import static dev.tvprobe.mediasession.ProbeSupport.*;
import android.graphics.Bitmap;
import android.media.MediaDescription;
import android.media.MediaMetadata;
import android.media.Rating;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Bundle;
import android.os.Parcel;
import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import java.lang.reflect.Array;
import java.util.ArrayList;
import java.util.List;

/** Bounded copy of callback-owned Android values. Never passes Android objects to the writer. */
final class MediaCodec {
    final Budget budget=new Budget();
    int readErrors;
    Object value(Object v) { return value(v,0); }
    private Object value(Object v,int depth) {
        if (v==null) return JSONObject.NULL;
        if (!budget.take(depth)) return obj("omitted",true,"reason","depth_or_node_budget");
        if (v instanceof CharSequence) return budget.text((CharSequence)v);
        if (v instanceof Boolean || v instanceof Number) return v;
        if (v instanceof Bundle) {
            JSONObject out=new JSONObject(); Bundle b=(Bundle)v;
            try {
                // Read identity first when it is published. Do not sort/copy an unbounded key set.
                List<String> keys=new ArrayList<>();
                String[] preferred={MediaMetadata.METADATA_KEY_MEDIA_ID,
                    "com.amazon.alexa.externalmediaplayer.metadata.PLAYBACK_SOURCE_ID",
                    "com.amazon.media.MEDIA_ID","com.amazon.media.CONTENT_ID",MediaMetadata.METADATA_KEY_TITLE,
                    MediaMetadata.METADATA_KEY_DISPLAY_TITLE,MediaMetadata.METADATA_KEY_DURATION};
                for (String k:preferred) if (b.containsKey(k)) keys.add(k);
                for (String k:b.keySet()) {
                    if (keys.size()>=Budget.MAX_ITEMS) break;
                    if (!keys.contains(k)) keys.add(k);
                }
                for (String k:keys) {
                    if (budget.exhausted()) { budget.mark("bundle_budget"); break; }
                    if (k.length()>256) { budget.mark("bundle_key_limit"); continue; }
                    try { put(out,k,value(b.get(k),depth+1)); }
                    catch (Exception e) { readErrors++; put(out,k,obj("readError",error(e))); }
                }
                if (keys.size()<b.size()) budget.mark("bundle_item_limit");
            } catch (Exception e) { readErrors++; put(out,"_bundleReadError",error(e)); }
            return out;
        }
        if (v instanceof Bitmap) {
            Bitmap b=(Bitmap)v;
            return obj("type","android.graphics.Bitmap","width",b.getWidth(),"height",b.getHeight(),"pixelsOmitted",true);
        }
        if (v instanceof Rating) {
            Rating r=(Rating)v;
            return obj("type","android.media.Rating","style",r.getRatingStyle(),"rated",r.isRated(),
                "heart",r.hasHeart(),"thumbUp",r.isThumbUp(),"stars",r.getStarRating(),"percent",r.getPercentRating());
        }
        if (v instanceof byte[]) {
            byte[] original=(byte[])v; int n=Math.min(4096,Math.min(original.length,Math.max(0,budget.chars/2)));
            byte[] prefix=new byte[n]; System.arraycopy(original,0,prefix,0,n); budget.chars-=n*2;
            if (n<original.length) budget.mark("byte_array_limit");
            return obj("type","byte[]","base64",Base64.encodeToString(prefix,Base64.NO_WRAP),
                "originalBytes",original.length,"truncated",n<original.length);
        }
        if (v instanceof Iterable) {
            JSONArray a=new JSONArray(); int n=0;
            for (Object item:(Iterable<?>)v) {
                if (n++>=Budget.MAX_ITEMS || budget.exhausted()) { a.put(budget.marker("iterable_limit")); break; }
                a.put(value(item,depth+1));
            }
            return a;
        }
        if (v.getClass().isArray()) {
            JSONArray a=new JSONArray(); int length=Array.getLength(v),n=0;
            for (;n<length && n<Budget.MAX_ITEMS && !budget.exhausted();n++) a.put(value(Array.get(v,n),depth+1));
            if (n<length) a.put(budget.marker("array_limit"));
            return a;
        }
        // Known Uri.toString() values are useful; do not invoke arbitrary Parcelable.toString().
        if (v instanceof android.net.Uri) return budget.text(v.toString());
        budget.mark("unsupported_object_text");
        return obj("type",v.getClass().getName(),"textOmitted",true);
    }
    Object metadata(MediaMetadata m) {
        if (m==null) return JSONObject.NULL;
        Parcel p=Parcel.obtain();
        try {
            m.writeToParcel(p,0); p.setDataPosition(0);
            Bundle b=p.readBundle(MediaCodec.class.getClassLoader());
            Object entries=value(b);
            JSONArray keys=new JSONArray();
            for (String k:m.keySet()) {
                if (keys.length()>=Budget.MAX_ITEMS || budget.exhausted()) { budget.mark("metadata_key_limit"); break; }
                keys.put(budget.text(k));
            }
            return obj("keys",keys,"entries",entries,"description",description(m.getDescription()));
        } catch (Exception e) { readErrors++; return obj("decodeError",error(e)); }
        finally { p.recycle(); }
    }
    Object state(PlaybackState s) {
        if (s==null) return JSONObject.NULL;
        JSONArray actions=new JSONArray();
        for (PlaybackState.CustomAction a:s.getCustomActions()) {
            if (actions.length()>=Budget.MAX_ITEMS || budget.exhausted()) { actions.put(budget.marker("custom_action_limit")); break; }
            actions.put(obj("action",value(a.getAction()),"name",value(a.getName()),"icon",a.getIcon(),"extras",value(a.getExtras())));
        }
        String[] names={"NONE","STOPPED","PAUSED","PLAYING","FAST_FORWARDING","REWINDING","BUFFERING","ERROR","CONNECTING","SKIPPING_TO_PREVIOUS","SKIPPING_TO_NEXT","SKIPPING_TO_QUEUE_ITEM"};
        int n=s.getState();
        return obj("state",n,"stateName",n>=0 && n<names.length?names[n]:"UNKNOWN",
            "positionMs",s.getPosition(),"bufferedPositionMs",s.getBufferedPosition(),"speed",s.getPlaybackSpeed(),
            "updatedElapsedRealtimeMs",s.getLastPositionUpdateTime(),"actions",s.getActions(),
            "activeQueueItemId",s.getActiveQueueItemId(),"errorMessage",value(s.getErrorMessage()),
            "extras",value(s.getExtras()),"customActions",actions);
    }
    Object description(MediaDescription d) {
        if (d==null) return JSONObject.NULL;
        return obj("mediaId",value(d.getMediaId()),"title",value(d.getTitle()),"subtitle",value(d.getSubtitle()),
            "description",value(d.getDescription()),"iconUri",value(d.getIconUri()),"iconBitmap",value(d.getIconBitmap()),
            "mediaUri",Build.VERSION.SDK_INT>=23?value(d.getMediaUri()):null,"extras",value(d.getExtras()));
    }
    Object queue(List<MediaSession.QueueItem> q) {
        if (q==null) return JSONObject.NULL;
        JSONArray out=new JSONArray(); int n=0;
        for (MediaSession.QueueItem i:q) {
            if (n++>=Budget.MAX_ITEMS || budget.exhausted()) { out.put(budget.marker("queue_limit")); break; }
            if (i==null) out.put(JSONObject.NULL);
            else out.put(obj("queueId",i.getQueueId(),"description",description(i.getDescription())));
        }
        return out;
    }
    Object info(MediaController.PlaybackInfo i) {
        if (i==null) return JSONObject.NULL;
        return obj("playbackType",i.getPlaybackType(),"volumeControl",i.getVolumeControl(),"currentVolume",i.getCurrentVolume(),
            "maxVolume",i.getMaxVolume(),"audioAttributes",value(String.valueOf(i.getAudioAttributes())));
    }
}

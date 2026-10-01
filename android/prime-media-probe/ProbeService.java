package dev.tvprobe.mediasession;

import static dev.tvprobe.mediasession.ProbeSupport.*;
import android.content.ComponentName;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.service.notification.NotificationListenerService;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.FileDescriptor;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/** Read-only observer. All observation ordering belongs to collector; disk work belongs to journal. */
public class ProbeService extends NotificationListenerService {
    private static final String TARGET="com.amazon.firebat";
    private static final long POLL_MS=1000, HEARTBEAT_MS=15000, DUMP_TIMEOUT_MS=2000;
    private final Clock clock=new Clock() {
        public long wall() { return System.currentTimeMillis(); }
        public long elapsed() { return SystemClock.elapsedRealtime(); }
    };
    private HandlerThread collectorThread;
    private Handler collector;
    private MediaSessionManager manager;
    private AsyncJournal journal;
    private String instance;
    private Stamp createdAt, connectedAt, lastCallbackAt;
    private final Lifetimes<MediaSession.Token> lifetimes=new Lifetimes<>();
    private final Map<MediaSession.Token,Watch> watches=new LinkedHashMap<>();
    private final Set<MediaSession.Token> destroyedTokens=new HashSet<>();
    private final List<JSONObject> pendingRemovals=new ArrayList<>();
    private final Operation activeRegistration=new Operation(), callbackRegistration=new Operation();
    private final Operation sessionReads=new Operation(), pollHealth=new Operation(), cleanupHealth=new Operation();
    private boolean connected, activeRegistered, closing;
    private long connectionEpoch, callbackCount, lateCallbacks, suppressedPolls, lastRecordAt, nextPollDue;
    private String lastSemantic;
    private JSONObject collectorError;
    private volatile String cachedDump;

    private final MediaSessionManager.OnActiveSessionsChangedListener listener=new MediaSessionManager.OnActiveSessionsChangedListener() {
        @Override public void onActiveSessionsChanged(List<MediaController> ignored) {
            Stamp received=new Stamp(clock); callbackCount++; lastCallbackAt=received;
            // Re-query on the collector so a delayed active-list callback cannot resurrect an old watch.
            safely(new Runnable() { public void run() { observe("sessions_changed",null,received,false); } });
        }
    };
    private final Runnable poll=new Runnable() {
        @Override public void run() {
            if (!connected || closing) return;
            pollHealth.attempt(clock);
            try {
                ensureActiveRegistration();
                Snapshot result=observe("poll",null,null,true);
                if (result.success) pollHealth.success(clock);
                else pollHealth.fail(clock,new IllegalStateException("Session acquisition incomplete"));
                publish(result,null);
            } catch (Exception e) {
                pollHealth.fail(clock,e); collectorError=error(e);
                record("poll_error",error(e),null,unknown("poll_error"));
            } finally {
                if (connected && !closing) { nextPollDue=clock.elapsed()+POLL_MS; collector.postDelayed(this,POLL_MS); }
            }
        }
    };
    @Override public void onCreate() {
        super.onCreate(); instance=UUID.randomUUID().toString(); createdAt=new Stamp(clock);
        manager=(MediaSessionManager)getSystemService(MEDIA_SESSION_SERVICE);
        journal=new AsyncJournal(instance,clock,new AsyncJournal.Files(getFilesDir()),AsyncJournal.QUEUE_RECORDS);
        collectorThread=new HandlerThread("PVProbe-collector"); collectorThread.start();
        collector=new Handler(collectorThread.getLooper());
        collector.post(new Runnable() { public void run() {
            Snapshot initial=unknown("listener_not_connected");
            record("service_created",obj("serviceCreatedAt",createdAt.json()),null,initial); publish(initial,null);
        } });
    }
    private ComponentName component() { return new ComponentName(this,ProbeService.class); }
    private void safely(Runnable work) {
        try { work.run(); }
        catch (Exception e) {
            collectorError=error(e); Log.e("PVProbe","Collection failed",e);
            Snapshot unavailable=unknown("collector_error");
            record("collector_error",error(e),null,unavailable); publish(unavailable,null);
        }
    }
    @Override public void onListenerConnected() {
        collector.post(new Runnable() { public void run() { safely(new Runnable() { public void run() {
            stopWatching("listener_reconnected"); connected=true; connectionEpoch++; connectedAt=new Stamp(clock);
            ensureActiveRegistration(); observe("connected",null,null,false); collector.post(poll);
        } }); } });
    }
    @Override public void onListenerDisconnected() {
        collector.post(new Runnable() { public void run() { safely(new Runnable() { public void run() {
            stopWatching("listener_disconnected");
            Snapshot current=unknown("listener_disconnected"); flushRemovals(current);
            record("disconnected",null,null,current); publish(current,null);
        } }); } });
    }
    @Override public void onDestroy() {
        collector.post(new Runnable() { public void run() {
            try {
                closing=true; stopWatching("service_destroyed");
                Snapshot current=unknown("service_destroyed"); flushRemovals(current);
                record("service_destroyed",null,null,current); publish(current,null);
            } finally { journal.close(); collectorThread.quitSafely(); }
        } });
        super.onDestroy();
    }
    private void ensureActiveRegistration() {
        if (!connected || activeRegistered) return;
        activeRegistration.attempt(clock);
        try {
            manager.addOnActiveSessionsChangedListener(listener,component(),collector);
            activeRegistered=true; activeRegistration.success(clock);
        } catch (Exception e) { activeRegistration.fail(clock,e); }
    }
    private void stopWatching(String cause) {
        connected=false; collector.removeCallbacks(poll); nextPollDue=0;
        if (manager!=null) {
            cleanupHealth.attempt(clock);
            try { manager.removeOnActiveSessionsChangedListener(listener); cleanupHealth.success(clock); }
            catch (Exception e) { cleanupHealth.fail(clock,e); }
        }
        activeRegistered=false;
        for (Watch watch:new ArrayList<>(watches.values())) remove(watch,cause,new Stamp(clock));
        destroyedTokens.clear(); lastSemantic=null;
    }
    private void remove(Watch watch,String cause,Stamp at) {
        if (watches.get(watch.controller.getSessionToken())!=watch) return;
        watches.remove(watch.controller.getSessionToken());
        JSONObject removed=lifetimes.remove(watch.controller.getSessionToken(),cause,at);
        cleanupHealth.attempt(clock);
        try { watch.controller.unregisterCallback(watch.callback); cleanupHealth.success(clock); }
        catch (Exception e) { cleanupHealth.fail(clock,e); }
        if (removed!=null) {
            put(removed,"sessionToken",watch.tokenHash);
            put(removed,"connectionEpoch",watch.epoch);
            put(removed,"serviceInstanceId",instance); pendingRemovals.add(removed);
        }
    }
    private void refresh(List<MediaController> list) {
        Map<MediaSession.Token,MediaController> found=new LinkedHashMap<>();
        if (list==null) throw new IllegalStateException("Active session list was null");
        if (list.size()>128) throw new IllegalStateException("Active session enumeration exceeds 128");
        for (MediaController c:list) if (TARGET.equals(c.getPackageName())) found.put(c.getSessionToken(),c);
        if (found.size()>16) throw new IllegalStateException("Target session count exceeds 16");
        for (Watch watch:new ArrayList<>(watches.values()))
            if (!found.containsKey(watch.controller.getSessionToken())) remove(watch,"active_list_removed",new Stamp(clock));
        destroyedTokens.retainAll(found.keySet());
        for (Map.Entry<MediaSession.Token,MediaController> entry:found.entrySet()) {
            if (destroyedTokens.contains(entry.getKey())) continue;
            Watch watch=watches.get(entry.getKey());
            if (watch==null) {
                watch=new Watch(entry.getValue(),lifetimes.add(entry.getKey())); watches.put(entry.getKey(),watch);
            }
            watch.register();
        }
    }
    private interface ReadValue { Object get(MediaCodec codec); }
    private final class Watch {
        final MediaController controller;
        final Lifetime lifetime;
        final MediaController.Callback callback;
        final String tokenHash;
        final long epoch;
        final JSONObject lastChangeTimes=new JSONObject();
        boolean registered;
        String lastDataSignature;
        Stamp lastObservedChange;
        Watch(MediaController controller,Lifetime lifetime) {
            this.controller=controller; this.lifetime=lifetime;
            epoch=connectionEpoch;
            tokenHash=Integer.toHexString(controller.getSessionToken().hashCode());
            callback=new MediaController.Callback() {
                @Override public void onMetadataChanged(final MediaMetadata m) { event("metadata_changed",new ReadValue() { public Object get(MediaCodec c) { return c.metadata(m); } }); }
                @Override public void onPlaybackStateChanged(final PlaybackState s) { event("playback_state_changed",new ReadValue() { public Object get(MediaCodec c) { return c.state(s); } }); }
                @Override public void onExtrasChanged(final Bundle b) { event("extras_changed",new ReadValue() { public Object get(MediaCodec c) { return c.value(b); } }); }
                @Override public void onQueueChanged(final List<MediaSession.QueueItem> q) { event("queue_changed",new ReadValue() { public Object get(MediaCodec c) { return c.queue(q); } }); }
                @Override public void onQueueTitleChanged(final CharSequence t) { event("queue_title_changed",new ReadValue() { public Object get(MediaCodec c) { return c.value(t); } }); }
                @Override public void onSessionEvent(final String name,final Bundle b) { event("session_event",new ReadValue() { public Object get(MediaCodec c) { return obj("name",c.value(name),"extras",c.value(b)); } }); }
                @Override public void onAudioInfoChanged(final MediaController.PlaybackInfo i) { event("audio_info_changed",new ReadValue() { public Object get(MediaCodec c) { return c.info(i); } }); }
                @Override public void onSessionDestroyed() {
                    final Stamp received=new Stamp(clock); callbackCount++; lastCallbackAt=received;
                    safely(new Runnable() { public void run() {
                        if (!lifetime.active) { lateCallbacks++; return; }
                        destroyedTokens.add(controller.getSessionToken()); remove(Watch.this,"session_destroyed",received);
                        observe("session_destroyed",obj("sessionInstanceId",lifetime.id,"sessionToken",tokenHash,"value",null),received,false);
                    } });
                }
            };
        }
        void register() {
            if (registered || !connected) return;
            callbackRegistration.attempt(clock);
            try { controller.registerCallback(callback,collector); registered=true; callbackRegistration.success(clock); }
            catch (Exception e) { callbackRegistration.fail(clock,e); }
        }
        void event(final String name,final ReadValue supplier) {
            final Stamp received=new Stamp(clock); callbackCount++; lastCallbackAt=received;
            safely(new Runnable() { public void run() {
                MediaCodec codec=new MediaCodec(); Object value;
                try { value=supplier.get(codec); } catch (Exception e) { value=obj("readError",error(e)); }
                JSONObject payload=obj("sessionInstanceId",lifetime.id,"sessionToken",tokenHash,"value",value,
                    "serialization",codec.budget.json(),"historical",!lifetime.active);
                if (lifetime.active) put(lastChangeTimes,name,received.json()); else lateCallbacks++;
                observe(lifetime.active?name:"late_callback",lifetime.active?payload:obj("originalReason",name,"payload",payload),received,false);
            } });
        }
    }
    private final class Snapshot {
        final JSONObject json; final String semantic; final boolean success;
        Snapshot(JSONObject json,String semantic,boolean success) { this.json=json; this.semantic=semantic; this.success=success; }
    }
    private Snapshot unknown(String reason) {
        Stamp at=new Stamp(clock);
        JSONObject j=obj("listenerConnected",connected,"sessions",null,"error",obj("reason",reason),
            "acquisitionStart",at.json(),"acquisitionEnd",at.json(),"complete",false);
        return new Snapshot(j,reason,false);
    }
    private Snapshot capture() {
        Stamp start=new Stamp(clock); sessionReads.attempt(clock);
        try {
            refresh(manager.getActiveSessions(component()));
            JSONArray sessions=new JSONArray(), comparable=new JSONArray(); boolean complete=true;
            for (Watch watch:watches.values()) {
                JSONObject row=read(watch); sessions.put(row);
                JSONObject semantic=copy(row); semantic.remove("acquisitionStart"); semantic.remove("acquisitionEnd");
                semantic.remove("lastChangeTimes"); semantic.remove("lastObservedChangeAt"); comparable.put(semantic);
                if (!row.optBoolean("dataComplete")) complete=false;
            }
            if (complete) sessionReads.success(clock);
            else sessionReads.fail(clock,new IllegalStateException("Session acquisition incomplete: getter, decode, or serialization limit"));
            JSONObject j=obj("listenerConnected",connected,"sessions",sessions,"complete",complete,
                "acquisitionStart",start.json(),"acquisitionEnd",new Stamp(clock).json());
            return new Snapshot(j,obj("listenerConnected",connected,"sessions",comparable).toString(),complete);
        } catch (Exception e) {
            sessionReads.fail(clock,e);
            JSONObject j=obj("listenerConnected",connected,"sessions",null,"complete",false,"error",error(e),
                "acquisitionStart",start.json(),"acquisitionEnd",new Stamp(clock).json());
            return new Snapshot(j,error(e).toString(),false);
        }
    }
    private void field(JSONObject row,JSONObject errors,String key,MediaCodec codec,ReadValue read) {
        try { put(row,key,read.get(codec)); }
        catch (Exception e) { put(errors,key,error(e)); }
    }
    private JSONObject read(final Watch watch) {
        Stamp start=new Stamp(clock); final MediaController c=watch.controller;
        MediaCodec codec=new MediaCodec(); JSONObject errors=new JSONObject();
        JSONObject row=obj("package",TARGET,"sessionInstanceId",watch.lifetime.id,"sessionToken",watch.tokenHash,"connectionEpoch",watch.epoch);
        // Capture core identity/state before optional queue/artwork/extras consume the construction budget.
        // Reserve part of the shared budget for the separately published session-extra identifiers.
        codec.budget.chars-=8192; codec.budget.nodes-=256;
        field(row,errors,"metadata",codec,new ReadValue() { public Object get(MediaCodec b) { return b.metadata(c.getMetadata()); } });
        field(row,errors,"playbackState",codec,new ReadValue() { public Object get(MediaCodec b) { return b.state(c.getPlaybackState()); } });
        codec.budget.chars+=8192; codec.budget.nodes+=256;
        field(row,errors,"sessionExtras",codec,new ReadValue() { public Object get(MediaCodec b) { return b.value(c.getExtras()); } });
        field(row,errors,"queueTitle",codec,new ReadValue() { public Object get(MediaCodec b) { return b.value(c.getQueueTitle()); } });
        field(row,errors,"queue",codec,new ReadValue() { public Object get(MediaCodec b) { return b.queue(c.getQueue()); } });
        field(row,errors,"flags",codec,new ReadValue() { public Object get(MediaCodec b) { return c.getFlags(); } });
        field(row,errors,"ratingType",codec,new ReadValue() { public Object get(MediaCodec b) { return c.getRatingType(); } });
        field(row,errors,"playbackInfo",codec,new ReadValue() { public Object get(MediaCodec b) { return b.info(c.getPlaybackInfo()); } });
        boolean succeeded=errors.length()==0 && codec.readErrors==0;
        put(row,"readErrors",errors); put(row,"nestedReadErrors",codec.readErrors);
        put(row,"readSucceeded",succeeded); put(row,"serialization",codec.budget.json());
        put(row,"dataComplete",succeeded && codec.budget.omitted==0);
        String signature=row.toString();
        if (succeeded && !signature.equals(watch.lastDataSignature)) {
            watch.lastDataSignature=signature; watch.lastObservedChange=new Stamp(clock);
        }
        put(row,"lastObservedChangeAt",Operation.json(watch.lastObservedChange));
        put(row,"acquisitionStart",start.json()); put(row,"acquisitionEnd",new Stamp(clock).json());
        put(row,"lastChangeTimes",copy(watch.lastChangeTimes));
        if (succeeded) watch.lifetime.remember(row);
        return row;
    }
    private Snapshot observe(String reason,Object payload,Stamp received,boolean isPoll) {
        Snapshot current=capture(); flushRemovals(current);
        if (isPoll && current.semantic.equals(lastSemantic) && clock.elapsed()-lastRecordAt<HEARTBEAT_MS) suppressedPolls++;
        else record(isPoll && current.semantic.equals(lastSemantic)?"heartbeat":reason,payload,received,current);
        lastSemantic=current.semantic; publish(current,null); return current;
    }
    private void flushRemovals(Snapshot current) {
        for (JSONObject removed:pendingRemovals) record("session_removed",removed,null,current);
        pendingRemovals.clear();
    }
    private JSONObject collectionHealth() {
        boolean all=activeRegistered; JSONArray registration=new JSONArray();
        for (Watch w:watches.values()) {
            all &= w.registered;
            registration.put(obj("sessionInstanceId",w.lifetime.id,"registrationCallSucceeded",w.registered));
        }
        return obj("listenerConnected",connected,"activeSessionsListenerRegistered",activeRegistered,"callbacksRegistered",all,
            "registrationMeans","registration_call_returned_without_exception",
            "activeRegistration",activeRegistration.json(),"callbackRegistration",callbackRegistration.json(),
            "sessionRegistrations",registration,"sessionReads",sessionReads.json(),"poll",pollHealth.json(),
            "lastSuccessfulSessionReadElapsedMs",sessionReads.lastSuccess==null?null:sessionReads.lastSuccess.elapsed,
            "lastPollAttemptElapsedMs",pollHealth.lastAttempt==null?null:pollHealth.lastAttempt.elapsed,
            "lastPollSuccessElapsedMs",pollHealth.lastSuccess==null?null:pollHealth.lastSuccess.elapsed,
            "pollExpected",connected && !closing,"nextPollDueElapsedMs",nextPollDue==0?null:nextPollDue,
            "pollOverdueMs",nextPollDue==0?null:Math.max(0,clock.elapsed()-nextPollDue),
            "callbackCount",callbackCount,"lastCallbackReceivedAt",Operation.json(lastCallbackAt),
            "lateCallbacks",lateCallbacks,"suppressedUnchangedPolls",suppressedPolls,
            "cleanup",cleanupHealth.json(),"lastCollectorError",collectorError);
    }
    private JSONObject envelope(Snapshot snapshot) {
        Stamp at=new Stamp(clock);
        return obj("schemaVersion",SCHEMA,"probeBuild",BUILD,"serviceInstanceId",instance,"connectionEpoch",connectionEpoch,
            "serviceCreatedAt",createdAt.json(),"connectionStartedAt",Operation.json(connectedAt),"wallTimeMs",at.wall,"elapsedRealtimeMs",at.elapsed,
            "snapshotAtomic",false,"snapshot",snapshot.json,"collectionHealth",collectionHealth());
    }
    private void record(String reason,Object payload,Stamp received,Snapshot current) {
        long seq=journal.next(); JSONObject row=envelope(current);
        put(row,"sequence",seq); put(row,"reason",reason); put(row,"eventPayload",payload);
        put(row,"callbackReceivedAt",Operation.json(received));
        JSONObject health=journal.health();
        put(row,"latestProducedSequence",seq); put(row,"latestWrittenSequence",health.opt("latestWrittenSequence"));
        put(row,"journalHealth",health);
        journal.offer(seq,row); lastRecordAt=clock.elapsed();
        Log.i("PVProbe","instance="+instance+" sequence="+seq+" reason="+reason);
    }
    private JSONObject checkpoint() {
        JSONObject health=journal.health();
        return obj("serviceInstanceId",instance,"latestProducedSequence",health.opt("latestProducedSequence"),
            "latestWrittenSequence",health.opt("latestWrittenSequence"),"capturedAt",new Stamp(clock).json());
    }
    private JSONObject publish(Snapshot current,JSONObject before) {
        JSONObject row=envelope(current),after=checkpoint();
        put(row,"checkpointBeforeSnapshot",before); put(row,"checkpointAfterSnapshot",after);
        put(row,"latestProducedSequence",after.opt("latestProducedSequence"));
        put(row,"latestWrittenSequence",after.opt("latestWrittenSequence")); put(row,"journalHealth",journal.health());
        put(row,"snapshotIsCached",false); cachedDump=boundedDump(row).toString(); return row;
    }
    private JSONObject boundedDump(JSONObject row) {
        if (row.toString().getBytes(StandardCharsets.UTF_8).length<=AsyncJournal.MAX_RECORD_BYTES) return row;
        JSONObject small=copy(row); put(small,"snapshot",null); put(small,"recordTruncated",true);
        put(small,"omissionReason","dump_byte_limit"); return small;
    }
    @Override protected void dump(FileDescriptor fd,PrintWriter writer,String[] args) {
        final Stamp requested=new Stamp(clock);
        final AtomicBoolean cancelled=new AtomicBoolean(); final CountDownLatch done=new CountDownLatch(1);
        final AtomicReference<JSONObject> result=new AtomicReference<>();
        final AtomicReference<JSONObject> failure=new AtomicReference<>();
        collector.post(new Runnable() { public void run() {
            try {
                if (cancelled.get()) return;
                JSONObject before=checkpoint(); Snapshot current=capture(); flushRemovals(current);
                result.set(publish(current,before));
            } catch (Exception e) { collectorError=error(e); failure.set(error(e)); }
            finally { done.countDown(); }
        } });
        boolean completed=false;
        try { completed=done.await(DUMP_TIMEOUT_MS,TimeUnit.MILLISECONDS); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        JSONObject row=result.get();
        if (!completed || row==null) {
            cancelled.set(true);
            try { row=cachedDump==null?obj("schemaVersion",SCHEMA,"probeBuild",BUILD,"serviceInstanceId",instance,"snapshot",null):new JSONObject(cachedDump); }
            catch (Exception e) { row=obj("schemaVersion",SCHEMA,"serviceInstanceId",instance,"snapshot",null); }
            put(row,"snapshotIsCached",true); put(row,"collectorStalledOrBusy",!completed);
            put(row,"journalHealth",journal.health());
            put(row,"outOfBandCheckpoint",obj("latestProducedSequence",journal.produced(),"capturedAt",new Stamp(clock).json()));
        }
        put(row,"dumpTimedOut",!completed); put(row,"dumpFailed",completed && result.get()==null);
        put(row,"dumpError",failure.get()); put(row,"dumpRequestedAt",requested.json());
        put(row,"dumpResponseAt",new Stamp(clock).json()); writer.println(boundedDump(row).toString());
    }
}

package dev.tvprobe.mediasession;

import static dev.tvprobe.mediasession.ProbeSupport.*;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;

/** Single ordered writer. No file access is performed by offer() or health(). */
final class AsyncJournal {
    static final int MAX_RECORD_BYTES = 256 * 1024, QUEUE_RECORDS = 64;
    static final long FILE_BYTES = 8L * 1024 * 1024;
    interface Sink {
        void initialize() throws Exception;
        void append(byte[] bytes, String instance, long sequence) throws Exception;
        JSONObject retention();
    }
    static final class Item {
        final long sequence; final byte[] bytes; final long queuedAt;
        Item(long s, byte[] b, long t) { sequence=s; bytes=b; queuedAt=t; }
    }
    private final String instance;
    private final Clock clock;
    private final Sink sink;
    private final ArrayBlockingQueue<Item> queue;
    private final Thread worker;
    private volatile boolean closing;
    private boolean initialized, lastWriteSucceeded;
    private long produced, written, dropped, writeFailures, rotationFailures, oversized;
    private long pendingSequence, pendingSince, lastWrite, lastAttempt, lostRangeDetails;
    private JSONObject lastError;
    private final List<JSONObject> losses = new ArrayList<>();
    AsyncJournal(String id, Clock clock, Sink sink, int capacity) {
        this.instance=id; this.clock=clock; this.sink=sink;
        queue=new ArrayBlockingQueue<>(capacity);
        worker=new Thread(new Runnable() { public void run() { work(); } }, "PVProbe-journal");
        worker.setDaemon(true); worker.start();
    }
    synchronized long next() { return ++produced; }
    synchronized long produced() { return produced; }
    void offer(long sequence, JSONObject record) {
        byte[] bytes=(record.toString()+"\n").getBytes(StandardCharsets.UTF_8);
        if (bytes.length > MAX_RECORD_BYTES) {
            synchronized (this) { oversized++; }
            // A diagnostic still occupies this sequence; the original payload is explicitly unavailable.
            JSONObject small=obj("schemaVersion", SCHEMA, "probeBuild", BUILD,
                "serviceInstanceId", instance, "sequence", sequence,
                "connectionEpoch", record.opt("connectionEpoch"),
                "serviceCreatedAt", record.opt("serviceCreatedAt"),
                "connectionStartedAt", record.opt("connectionStartedAt"),
                "wallTimeMs", record.opt("wallTimeMs"), "elapsedRealtimeMs", record.opt("elapsedRealtimeMs"),
                "callbackReceivedAt", record.opt("callbackReceivedAt"),
                "latestProducedSequence", sequence, "latestWrittenSequence", record.opt("latestWrittenSequence"),
                "collectionHealth", record.opt("collectionHealth"), "journalHealth", health(),
                "reason", record.opt("reason"), "recordTruncated", true,
                "originalEncodedBytes", bytes.length, "eventPayload", null, "snapshot", null,
                "omissionReason", "record_byte_limit", "snapshotAtomic", false);
            JSONObject payload=record.optJSONObject("eventPayload");
            if (payload!=null) put(small,"eventSessionInstanceId",payload.opt("sessionInstanceId"));
            bytes=(small.toString()+"\n").getBytes(StandardCharsets.UTF_8);
        }
        synchronized (this) {
            if (closing || !queue.offer(new Item(sequence, bytes, clock.elapsed())))
                loss(sequence, closing ? "writer_closed" : "queue_full");
        }
    }
    private void loss(long seq, String cause) {
        dropped++;
        JSONObject tail=losses.isEmpty()?null:losses.get(losses.size()-1);
        if (tail != null && cause.equals(tail.optString("reason")) && tail.optLong("lastSequence")+1==seq)
            put(tail,"lastSequence",seq);
        else {
            if (losses.size() == 64) { losses.remove(0); lostRangeDetails++; }
            losses.add(obj("serviceInstanceId",instance,"firstSequence",seq,"lastSequence",seq,"reason",cause));
        }
    }
    private void work() {
        while (!closing || !queue.isEmpty()) {
            Item item;
            try { item=queue.poll(100,TimeUnit.MILLISECONDS); }
            catch (InterruptedException e) { continue; }
            if (item == null) continue;
            synchronized (this) { pendingSequence=item.sequence; pendingSince=item.queuedAt; lastAttempt=clock.elapsed(); lastWriteSucceeded=false; }
            try {
                if (!initialized) { sink.initialize(); synchronized (this) { initialized=true; } }
                sink.append(item.bytes,instance,item.sequence);
                synchronized (this) { written=item.sequence; lastWrite=clock.elapsed(); lastWriteSucceeded=true; }
            } catch (Exception e) {
                synchronized (this) {
                    writeFailures++;
                    if (e instanceof RotationException) rotationFailures++;
                    lastError=obj("atElapsedRealtimeMs",clock.elapsed(),"sequence",item.sequence,"detail",error(e));
                    loss(item.sequence,"write_failure");
                }
            } finally { synchronized (this) { pendingSequence=0; pendingSince=0; } }
        }
    }
    synchronized JSONObject health() {
        Item oldest=queue.peek();
        long since=pendingSequence!=0?pendingSince:oldest==null?0:oldest.queuedAt;
        JSONArray ranges=new JSONArray(); for (JSONObject r:losses) ranges.put(copy(r));
        return obj("latestProducedSequence",produced,"latestWrittenSequence",written,
            "writerAlive",worker.isAlive(),"writerClosing",closing,"initialized",initialized,
            "queueDepth",queue.size(),"queueCapacity",queue.remainingCapacity()+queue.size(),
            "inFlightSequence",pendingSequence,"oldestPendingAgeMs",since==0?0:Math.max(0,clock.elapsed()-since),
            "lastJournalAttemptElapsedMs",lastAttempt==0?null:lastAttempt,
            "lastJournalWriteElapsedMs",lastWrite==0?null:lastWrite,
            "lastWriteSucceeded",lastWriteSucceeded,
            "droppedRecords",dropped,"writeFailures",writeFailures,"rotationFailures",rotationFailures,
            "oversizedRecords",oversized,"lossRanges",ranges,"lossRangeDetailsEvicted",lostRangeDetails,
            "lastError",lastError,"retention",sink.retention());
    }
    void close() { closing=true; }
    boolean await(long timeoutMs) throws InterruptedException { worker.join(timeoutMs); return !worker.isAlive(); }
    static final class RotationException extends IOException {
        private static final long serialVersionUID=1L;
        RotationException(String msg) { super(msg); }
    }

    /** Retained ranges describe successfully written records, scoped by service UUID. */
    static final class Ranges {
        final List<JSONObject> values=new ArrayList<>();
        long unattributed, invalid, evicted;
        void add(String instance,long seq) {
            if (instance==null || instance.isEmpty() || seq<1) { unattributed++; return; }
            JSONObject tail=values.isEmpty()?null:values.get(values.size()-1);
            if (tail!=null && instance.equals(tail.optString("serviceInstanceId")) && tail.optLong("lastSequence")+1==seq)
                put(tail,"lastSequence",seq);
            else {
                if (values.size()==64) { values.remove(0); evicted++; }
                values.add(obj("serviceInstanceId",instance,"firstSequence",seq,"lastSequence",seq));
            }
        }
        JSONObject json() {
            JSONArray a=new JSONArray(); for (JSONObject v:values) a.put(copy(v));
            return obj("ranges",a,"rangesComplete",evicted==0,"evictedRanges",evicted,
                "unattributedRecords",unattributed,"invalidRecords",invalid);
        }
    }
    static final class Files implements Sink {
        private final File current, previous;
        private final long limit;
        private Ranges active=new Ranges(), old=new Ranges();
        private volatile String published="{\"initialized\":false}";
        Files(File directory) { this(directory,FILE_BYTES); }
        Files(File directory,long limit) {
            current=new File(directory,"events.jsonl"); previous=new File(directory,"events.previous.jsonl"); this.limit=limit;
        }
        public void initialize() throws Exception {
            active=scan(current); old=scan(previous); publish();
        }
        private void publish() {
            published=obj("initialized",true,"current",active.json(),"previous",old.json(),
                "fileLimitBytes",limit,"maximumRecordBytes",MAX_RECORD_BYTES).toString();
        }
        public JSONObject retention() {
            try { return new JSONObject(published); } catch (Exception e) { throw new IllegalStateException(e); }
        }
        public void append(byte[] bytes,String instance,long seq) throws Exception {
            if (current.length()+bytes.length > limit && current.length()>0) {
                if (previous.exists() && !previous.delete()) throw new RotationException("Cannot remove previous journal");
                old=new Ranges(); publish();
                if (!current.renameTo(previous)) throw new RotationException("Cannot rotate current journal");
                old=active; active=new Ranges(); publish();
            }
            // Separate an interrupted legacy/failed append from the next JSON line.
            if (current.length()>0) {
                try (RandomAccessFile f=new RandomAccessFile(current,"rw")) {
                    f.seek(f.length()-1);
                    if (f.read()!=10) { f.seek(f.length()); f.write(10); f.getFD().sync(); }
                }
            }
            try (FileOutputStream f=new FileOutputStream(current,true)) {
                f.write(bytes); f.getFD().sync();
            } catch (Exception e) { active.invalid++; publish(); throw e; }
            active.add(instance,seq); publish();
        }
        private static Ranges scan(File path) throws IOException {
            Ranges ranges=new Ranges(); if (!path.exists()) return ranges;
            try (InputStream in=new BufferedInputStream(new FileInputStream(path))) {
                ByteArrayOutputStream line=new ByteArrayOutputStream(); boolean tooLarge=false; int b;
                while ((b=in.read())!=-1) {
                    if (b==10) {
                        if (tooLarge) ranges.invalid++;
                        else if (line.size()>0) {
                            try {
                                JSONObject row=new JSONObject(new String(line.toByteArray(),StandardCharsets.UTF_8));
                                ranges.add(row.optString("serviceInstanceId",""),row.optLong("sequence",0));
                            } catch (Exception e) { ranges.invalid++; }
                        }
                        line.reset(); tooLarge=false;
                    } else if (line.size()<MAX_RECORD_BYTES) line.write(b); else tooLarge=true;
                }
                if (line.size()>0 || tooLarge) ranges.invalid++;
            }
            return ranges;
        }
    }
}

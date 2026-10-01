package dev.tvprobe.mediasession;

import static dev.tvprobe.mediasession.ProbeSupport.*;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Fault tests execute production identity, health, limits, queue and disk code on a host JVM. */
public final class CoreTest {
    static int passed;
    static class TestClock implements Clock {
        long time=100; public long wall() { return 1800000000000L+time; } public long elapsed() { return time; }
    }
    static void check(boolean condition,String message) { if (!condition) throw new AssertionError(message); }
    interface Test { void run() throws Exception; }
    static void test(String name,Test test) throws Exception { test.run(); passed++; System.out.println("PASS "+name); }
    static class Token {
        final int value; Token(int value) { this.value=value; }
        @Override public int hashCode() { return 42; }
        @Override public boolean equals(Object o) { return o instanceof Token && ((Token)o).value==value; }
    }
    static class MemorySink implements AsyncJournal.Sink {
        final List<String> lines=Collections.synchronizedList(new ArrayList<String>());
        public void initialize() throws Exception { }
        public void append(byte[] bytes,String instance,long sequence) throws Exception { lines.add(new String(bytes,StandardCharsets.UTF_8)); }
        public JSONObject retention() { return obj("testSink",true); }
    }
    static JSONObject event(long seq,String id) { return obj("schemaVersion",2,"serviceInstanceId",id,"sequence",seq,"reason","test","snapshot",obj("sessions",new JSONArray())); }
    static void submit(AsyncJournal journal,String id) { long seq=journal.next(); journal.offer(seq,event(seq,id)); }
    static void finish(AsyncJournal j) throws Exception { j.close(); check(j.await(5000),"writer failed to drain"); }
    static Path directory() throws Exception { return Files.createTempDirectory("pvprobe-test-"); }
    static void append(AsyncJournal.Files sink,String id,long seq) throws Exception { sink.append((event(seq,id).toString()+"\n").getBytes(StandardCharsets.UTF_8),id,seq); }
    public static void main(String[] args) throws Exception {
        test("hash collisions, token equality and new lifetimes",()->{
            Lifetimes<Token> lifetimes=new Lifetimes<>();
            Lifetime a=lifetimes.add(new Token(1)),b=lifetimes.add(new Token(2));
            check(!a.id.equals(b.id),"hash collision aliased identities");
            check(a==lifetimes.add(new Token(1)),"equal token did not retain lifetime");
            lifetimes.remove(new Token(1),"active_list_removed",new Stamp(new TestClock()));
            check(!a.id.equals(lifetimes.add(new Token(1)).id),"re-add reused lifetime");
        });
        test("historical snapshot copy and original timestamps",()->{
            TestClock c=new TestClock(); Lifetime life=new Lifetime("s1");
            JSONObject original=obj("acquisitionStart",new Stamp(c).json(),"metadata",obj("id","event-A"));
            life.remember(original); original.getJSONObject("metadata").put("id","event-B"); c.time=900;
            JSONObject gone=life.finish("session_destroyed",new Stamp(c));
            check(gone.getJSONObject("lastKnownSnapshot").getJSONObject("metadata").getString("id").equals("event-A"),"snapshot mutated");
            check(gone.getJSONObject("lastKnownSnapshot").getJSONObject("acquisitionStart").getLong("elapsedRealtimeMs")==100,"history freshened");
            check(gone.getBoolean("historical") && !gone.getBoolean("eventCompletionInferred"),"disappearance inferred completion");
        });
        test("removal signals deduplicate in either order",()->{
            for (String first:new String[]{"active_list_removed","session_destroyed"}) {
                Lifetimes<Token> lives=new Lifetimes<>(); Token t=new Token(1); Lifetime w=lives.add(t);
                check(lives.remove(t,first,new Stamp(new TestClock()))!=null,"first removal lost");
                check(lives.remove(t,"second",new Stamp(new TestClock()))==null,"duplicate final record");
                check(w.finish("late_callback",new Stamp(new TestClock()))==null,"old callback finished twice");
            }
        });
        test("read success cannot clear registration failure",()->{
            TestClock c=new TestClock(); Operation registration=new Operation(),reads=new Operation();
            registration.attempt(c); registration.fail(c,new SecurityException("denied"));
            c.time=200; reads.attempt(c); reads.success(c);
            check(!registration.json().getBoolean("lastAttemptSucceeded"),"registration failure erased");
            check(reads.json().getBoolean("lastAttemptSucceeded"),"independent read failed");
            registration.attempt(c); registration.success(c);
            check(registration.json().getLong("failures")==1,"historical failure erased");
        });
        test("string and aggregate budgets preserve Unicode boundaries",()->{
            Budget b=new Budget(); StringBuilder huge=new StringBuilder();
            for(int n=0;n<4095;n++) huge.append('x'); huge.append("\ud83d\ude00more");
            JSONObject s=(JSONObject)b.text(huge);
            check(s.getString("prefix").length()==4095,"split surrogate");
            check(s.getInt("originalLength")==huge.length(),"lost original length");
            for(int n=0;n<50;n++) b.text(huge);
            check(b.chars>=0 && b.chars<=1,"aggregate character budget not enforced");
            for(int n=0;n<3000;n++) b.take(1);
            check(!b.take(1) && b.reasons.length()<=32,"unbounded diagnostic budget");
        });
        test("queue saturation and missing final record exposed without disk blocking",()->{
            TestClock c=new TestClock(); CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);
            MemorySink sink=new MemorySink() {
                @Override public void append(byte[] b,String id,long seq) throws Exception {
                    entered.countDown(); check(release.await(3,TimeUnit.SECONDS),"test release timed out"); super.append(b,id,seq);
                }
            };
            AsyncJournal j=new AsyncJournal("A",c,sink,2); submit(j,"A");
            check(entered.await(2,TimeUnit.SECONDS),"writer never began");
            submit(j,"A"); submit(j,"A"); submit(j,"A");
            JSONObject health=j.health();
            check(health.getLong("latestProducedSequence")==4 && health.getLong("latestWrittenSequence")==0,"checkpoints conflate queued/written");
            check(health.getLong("droppedRecords")==1 && health.getInt("queueDepth")==2,"overflow not explicit");
            check(health.getJSONArray("lossRanges").getJSONObject(0).getLong("firstSequence")==4,"terminal loss missing");
            release.countDown(); finish(j);
            check(j.health().getLong("latestWrittenSequence")==3,"dropped terminal sequence marked written");
        });
        test("append failure retains gap after later recovery",()->{
            MemorySink sink=new MemorySink() { @Override public void append(byte[] b,String id,long seq) throws Exception {
                if(seq==2) throw new IOException("disk fault"); super.append(b,id,seq);
            }};
            AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,8);
            submit(j,"A"); submit(j,"A"); submit(j,"A"); finish(j);
            JSONObject health=j.health();
            check(health.getLong("latestWrittenSequence")==3 && health.getLong("writeFailures")==1,"recovery checkpoints wrong");
            check(health.getBoolean("lastWriteSucceeded") && health.getLong("droppedRecords")==1,"history loss hidden after recovery");
            check(health.getJSONArray("lossRanges").getJSONObject(0).getLong("firstSequence")==2,"wrong loss range");
        });
        test("initialization failure is counted and retries",()->{
            MemorySink sink=new MemorySink() { int calls;
                @Override public void initialize() throws Exception { if(++calls==1) throw new IOException("scan error"); }
            };
            AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,8); submit(j,"A"); submit(j,"A"); finish(j);
            check(j.health().getLong("writeFailures")==1 && j.health().getLong("latestWrittenSequence")==2,"init fault lost");
        });
        test("off-thread writer receives immutable serialized observation",()->{
            CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);
            MemorySink sink=new MemorySink() { @Override public void append(byte[] b,String id,long seq) throws Exception {
                entered.countDown(); release.await(3,TimeUnit.SECONDS); super.append(b,id,seq);
            }};
            AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,8); long seq=j.next(); JSONObject row=event(seq,"A");
            j.offer(seq,row); entered.await(2,TimeUnit.SECONDS); row.put("reason","mutated"); release.countDown(); finish(j);
            check(new JSONObject(sink.lines.get(0)).getString("reason").equals("test"),"callback object leaked to writer");
        });
        test("oversize record becomes bounded explicit diagnostic at same sequence",()->{
            MemorySink sink=new MemorySink(); AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,8);
            long seq=j.next(); JSONObject row=event(seq,"A"); char[] chars=new char[300000]; Arrays.fill(chars,'a'); row.put("huge",new String(chars));
            j.offer(seq,row); finish(j); JSONObject actual=new JSONObject(sink.lines.get(0));
            check(actual.getBoolean("recordTruncated") && actual.getLong("sequence")==1,"oversize silently discarded");
            check(sink.lines.get(0).getBytes(StandardCharsets.UTF_8).length<=AsyncJournal.MAX_RECORD_BYTES,"record bound failed");
            check(j.health().getLong("oversizedRecords")==1,"missing oversize health");
        });
        test("restart scan retains instance-scoped sequence ranges",()->{
            Path root=directory(); AsyncJournal.Files sink=new AsyncJournal.Files(root.toFile()); sink.initialize(); append(sink,"A",1); append(sink,"A",2); append(sink,"B",1);
            AsyncJournal.Files restarted=new AsyncJournal.Files(root.toFile()); restarted.initialize();
            JSONArray ranges=restarted.retention().getJSONObject("current").getJSONArray("ranges");
            check(ranges.length()==2 && ranges.getJSONObject(0).getLong("lastSequence")==2,"cross-instance range coalesced");
            check(ranges.getJSONObject(1).getString("serviceInstanceId").equals("B"),"new instance attribution lost");
        });
        test("legacy and partial lines remain explicit and do not poison next append",()->{
            Path root=directory(); Files.write(root.resolve("events.jsonl"),"{\"sequence\":7}\n{\"partial\":".getBytes(StandardCharsets.UTF_8));
            AsyncJournal.Files sink=new AsyncJournal.Files(root.toFile()); sink.initialize(); append(sink,"A",1);
            JSONObject h=sink.retention().getJSONObject("current");
            check(h.getLong("unattributedRecords")==1 && h.getLong("invalidRecords")==1,"legacy/torn evidence hidden");
            List<String> lines=Files.readAllLines(root.resolve("events.jsonl"),StandardCharsets.UTF_8);
            check(new JSONObject(lines.get(lines.size()-1)).getLong("sequence")==1,"append concatenated partial line");
        });
        test("rotation preserves two bounded files and exact ranges",()->{
            Path root=directory(); AsyncJournal.Files sink=new AsyncJournal.Files(root.toFile(),380); sink.initialize();
            for(int n=1;n<=9;n++) append(sink,"A",n);
            check(Files.size(root.resolve("events.jsonl"))<=380 && Files.size(root.resolve("events.previous.jsonl"))<=380,"rotation exceeded file limit");
            JSONArray active=sink.retention().getJSONObject("current").getJSONArray("ranges");
            check(active.getJSONObject(active.length()-1).getLong("lastSequence")==9,"retained tail wrong");
        });
        test("real rotation failure is counted separately",()->{
            Path root=directory(); Files.write(root.resolve("events.jsonl"),new byte[400]);
            Path prior=Files.createDirectory(root.resolve("events.previous.jsonl")); Files.write(prior.resolve("block"),new byte[]{1});
            // Initialize before adding an obstructing directory so failure happens in rotation, not scan.
            AsyncJournal.Files files=new AsyncJournal.Files(root.toFile(),380);
            MemorySink fakeInit=new MemorySink() {
                @Override public void append(byte[] b,String id,long seq) throws Exception { files.append(b,id,seq); }
                @Override public JSONObject retention() { return files.retention(); }
            };
            AsyncJournal j=new AsyncJournal("A",new TestClock(),fakeInit,8); submit(j,"A"); finish(j);
            check(j.health().getLong("rotationFailures")==1 && j.health().getLong("droppedRecords")==1,"rotation fault hidden");
            check(Files.exists(root.resolve("events.jsonl")),"failed rotation destroyed current file");
        });
        test("retained range detail eviction is explicit",()->{
            AsyncJournal.Ranges ranges=new AsyncJournal.Ranges(); for(int n=1;n<150;n++) ranges.add("A",n*2);
            check(!ranges.json().getBoolean("rangesComplete") && ranges.values.size()==64,"range limit claims completeness");
        });
        test("draining close preserves accepted order and reports late offers",()->{
            MemorySink sink=new MemorySink(); AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,64);
            for(int n=1;n<=50;n++) submit(j,"A"); finish(j);
            check(sink.lines.size()==50,"close lost queue");
            for(int n=0;n<50;n++) check(new JSONObject(sink.lines.get(n)).getLong("sequence")==n+1,"writer reordered records");
            submit(j,"A"); check(j.health().getLong("droppedRecords")==1,"closed writer silently accepted");
        });
        test("pending operation differs from a completed failure",()->{
            TestClock c=new TestClock(); Operation op=new Operation(); op.attempt(c);
            check(op.json().getBoolean("inProgress") && op.json().getLong("failures")==0,"pending operation reported as failure");
            op.fail(c,new IOException("fault")); check(!op.json().getBoolean("inProgress"),"failed operation remains pending");
        });
        test("loss detail history is bounded without resetting cumulative count",()->{
            MemorySink sink=new MemorySink(); AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,8); finish(j);
            for(int n=0;n<80;n++) { j.next(); submit(j,"A"); }
            JSONObject h=j.health();
            check(h.getLong("droppedRecords")==80 && h.getJSONArray("lossRanges").length()==64,"loss counters or bound incorrect");
            check(h.getLong("lossRangeDetailsEvicted")==16,"loss detail eviction hidden");
        });
        test("encoded newline and Unicode payload remains a single journal record",()->{
            MemorySink sink=new MemorySink(); AsyncJournal j=new AsyncJournal("A",new TestClock(),sink,8);
            long seq=j.next(); JSONObject e=event(seq,"A"); e.put("value","line1\nline2 \ud83d\ude00"); j.offer(seq,e); finish(j);
            String line=sink.lines.get(0);
            check(line.indexOf('\n')==line.length()-1,"payload injected a JSONL record separator");
            check(new JSONObject(line).getString("value").equals("line1\nline2 \ud83d\ude00"),"payload changed");
        });
        System.out.println("Passed "+passed+" tests.");
    }
}

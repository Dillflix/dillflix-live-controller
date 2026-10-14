/* Installed once before engine execution. Never patch/unpatch while Prime runs. */
#include <gum/guminterceptor.h>
#include <gum/gumspinlock.h>
#include <glib.h>
typedef struct {
  GumSpinlock lock;
  volatile gint mode; /* 0 startup, 1 on-demand, 2 closed */
  volatile gint pending;
  volatile gint busy;
  gpointer owner;
  guint thread;
  guint generation;
  guint deliveries;
} State;
typedef struct { gpointer ctx; guint thread; guint generation; guint eligible; } Invocation;
extern State state;
extern void startup_callback(gpointer ctx, guint tid, guint is_eval, guint phase, guint tag);
extern void transfer_callback(gpointer ctx, guint generation);
guint state_size(void) { return sizeof(State); }
void init_gate(void) { gum_spinlock_init(&state.lock); }
static gint load(volatile gint *v) { return g_atomic_int_add(v, 0); }
static void set(volatile gint *v, gint n) { g_atomic_int_add(v, n-load(v)); }
void handoff(gpointer ctx, guint tid) {
  gum_spinlock_acquire(&state.lock);
  state.owner=ctx; state.thread=tid; set(&state.mode,1);
  gum_spinlock_release(&state.lock);
}
void arm(guint generation) {
  gum_spinlock_acquire(&state.lock);
  if(load(&state.mode)==1) { state.generation=generation; set(&state.pending,1); }
  gum_spinlock_release(&state.lock);
}
void disarm(void) {
  gum_spinlock_acquire(&state.lock);set(&state.pending,0);gum_spinlock_release(&state.lock);
}
void close_gate(void) {
  gum_spinlock_acquire(&state.lock);set(&state.pending,0);set(&state.mode,2);gum_spinlock_release(&state.lock);
}
static guint result_tag(GumInvocationContext *ic) {
#ifdef HAVE_ARM
  return ic->cpu_context->r[1];
#else
  /* Local host test uses ordinary scalar return values. Production requires ARM32. */
  return 0;
#endif
}
static gboolean claim_startup(void) {
  gboolean yes=FALSE;
  gum_spinlock_acquire(&state.lock);
  if(load(&state.mode)==0 && !load(&state.busy)) {set(&state.busy,1);yes=TRUE;}
  gum_spinlock_release(&state.lock);return yes;
}
void on_enter(GumInvocationContext *ic) {
  Invocation *v=GUM_IC_GET_INVOCATION_DATA(ic,Invocation);
  gint mode=load(&state.mode);
  v->eligible=0;
  if(mode==2 || load(&state.busy) || (mode==1 && !load(&state.pending)))return;
  v->ctx=gum_invocation_context_get_nth_argument(ic,0);
  v->thread=gum_invocation_context_get_thread_id(ic);
  if(mode==0) {
    v->eligible=1;
    if(GUM_IC_GET_FUNC_DATA(ic,gpointer)!=NULL && claim_startup()) {
      startup_callback(v->ctx,v->thread,1,0,0);set(&state.busy,0);
    }
  } else {
    gum_spinlock_acquire(&state.lock);
    if(load(&state.mode)==1 && load(&state.pending) && v->ctx==state.owner && v->thread==state.thread) {
      v->eligible=2;v->generation=state.generation;
    }
    gum_spinlock_release(&state.lock);
  }
}
void on_leave(GumInvocationContext *ic) {
  Invocation *v=GUM_IC_GET_INVOCATION_DATA(ic,Invocation);
  guint tag;
  gboolean deliver=FALSE;
  if(!v->eligible || load(&state.busy))return;
  tag=result_tag(ic);
  if(tag==6)return; /* Preserve the application's pending exception. */
  if(v->eligible==1) {
    if(claim_startup()) {
      startup_callback(v->ctx,v->thread,GUM_IC_GET_FUNC_DATA(ic,gpointer)!=NULL,1,tag);
      set(&state.busy,0);
    }
    return;
  }
  gum_spinlock_acquire(&state.lock);
  if(load(&state.mode)==1 && load(&state.pending) && !load(&state.busy) && v->generation==state.generation) {
    set(&state.pending,0);set(&state.busy,1);state.deliveries++;deliver=TRUE;
  }
  gum_spinlock_release(&state.lock);
  if(deliver) { transfer_callback(v->ctx,v->generation);set(&state.busy,0); }
}

// Polling and update notifications share one bounded read. A slow response must
// not be discarded just because another polling interval has elapsed.
export function overviewRead<T>(request: (signal: AbortSignal) => Promise<T>) {
  let active: { controller: AbortController; promise: Promise<T> } | null = null;
  return {
    read(): Promise<T> {
      if (active) return active.promise;
      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, 30000);
      const promise = request(controller.signal).catch((error) => {
        if (timedOut) throw new Error("The controller took too long to respond. Retrying automatically; you can also try again.");
        throw error;
      }).finally(() => {
        clearTimeout(timer);
        if (active?.controller === controller) active = null;
      });
      active = { controller, promise };
      return promise;
    },
    abort() {
      active?.controller.abort();
      active = null;
    },
  };
}

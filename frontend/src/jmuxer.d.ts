declare module "jmuxer" {
  export default class JMuxer {
    constructor(options: {
      node: HTMLVideoElement;
      mode: "video";
      flushingTime: number;
      maxDelay: number;
      clearBuffer: boolean;
      fps: number;
      onReady: () => void;
      onError: (error: { name?: string; error?: string }) => void;
      onKeyframePosition?: () => void;
      onUnsupportedCodec: () => void;
    });
    url?: string;
    // Pinned 2.1.4 cleanup index; corrected for variable frame durations.
    kfPosition: number[];
    feed(data: {
      video: Uint8Array;
      duration: number;
      isLastVideoFrameComplete: boolean;
    }): void;
    destroy(): void;
  }
}

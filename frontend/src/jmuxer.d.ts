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
      onError: () => void;
      onUnsupportedCodec: () => void;
    });
    url?: string;
    feed(data: {
      video: Uint8Array;
      duration: number;
      isLastVideoFrameComplete: boolean;
    }): void;
    destroy(): void;
  }
}

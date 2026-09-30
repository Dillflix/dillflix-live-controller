import { useRef, useState } from "react";
import { Download, Upload } from "lucide-react";
import { api } from "./api";
import type { ConfigurationDocument } from "./types";

export function ConfigurationTools({
  devicePath,
  busy,
  onPreview,
  onError,
}: {
  devicePath: string;
  busy: boolean;
  onPreview: (document: unknown) => Promise<void>;
  onError: (message: string) => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [working, setWorking] = useState(false);
  const download = async () => {
    setWorking(true);
    try {
      const document = await api<ConfigurationDocument>(
        devicePath + "/configuration",
      );
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(document, null, 2) + "\n"], {
          type: "application/json",
        }),
      );
      const link = window.document.createElement("a");
      link.href = url;
      link.download = `dillflix-${document.source_mode}-configuration.json`;
      window.document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setWorking(false);
    }
  };
  const read = async (file: File) => {
    setWorking(true);
    try {
      if (file.size > 1024 * 1024)
        throw new Error("Choose a configuration file smaller than 1 MiB.");
      let document: unknown;
      try {
        document = JSON.parse(await file.text());
      } catch {
        throw new Error(
          "This file is not valid JSON. Choose an exported Dillflix configuration.",
        );
      }
      await onPreview(document);
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setWorking(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };
  return (
    <section className="df-panel">
      <h2>Configuration backup</h2>
      <p className="df-subtitle">
        Export priorities, preferred teams, and settings. Review an import
        before applying it. Your watch plan stays in place.
      </p>
      <div className="df-row df-spacer df-wrap">
        <button
          className="df-button"
          type="button"
          disabled={busy || working}
          onClick={() => void download()}
        >
          <Download size={16} />
          Export configuration
        </button>
        <button
          className="df-button"
          type="button"
          disabled={busy || working}
          onClick={() => fileInput.current?.click()}
        >
          <Upload size={16} />
          Import configuration
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          className="df-screenreader"
          aria-label="Configuration file"
          tabIndex={-1}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void read(file);
          }}
        />
      </div>
    </section>
  );
}

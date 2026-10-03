import { useState } from "react";
import { Download } from "lucide-react";
import { api } from "./api";

export function DiagnosticsTools({
  devicePath,
  onError,
}: {
  devicePath: string;
  onError: (message: string) => void;
}) {
  const [working, setWorking] = useState(false);
  async function download() {
    setWorking(true);
    try {
      const bundle = await api<unknown>(devicePath + "/diagnostics");
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(bundle, null, 2) + "\n"], {
          type: "application/json",
        }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `dillflix-diagnostics-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setWorking(false);
    }
  }
  return (
    <button className="df-button" onClick={download} disabled={working}>
      <Download size={16} />
      {working ? "Collecting diagnostics…" : "Export diagnostics"}
    </button>
  );
}

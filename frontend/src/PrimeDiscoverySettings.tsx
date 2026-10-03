import { useEffect, useState } from "react";
import type { Device, Preferences } from "./types";

export function PrimeDiscoverySettings({
  device,
  busy,
  save,
  refresh,
}: {
  device: Device;
  busy: boolean;
  save: (value: Partial<Preferences>) => void;
  refresh: () => void;
}) {
  const settings = device.preferences.prime_discovery;
  const inventory = device.prime_pages;
  const [brief, setBrief] = useState(settings.brief);
  useEffect(() => setBrief(settings.brief), [settings.brief]);
  const pages = [...inventory.pages];
  for (const id of settings.enabled_pages)
    if (!pages.some((p) => p.id === id))
      pages.push({ id, title: id, available: false });
  const pending = ["queued", "refreshing"].includes(inventory.state);
  return (
    <section className="df-panel">
      <h2>Prime live discovery</h2>
      <p className="df-subtitle">
        When no scheduled event is selected, choose live sports from these
        pages. Page choices apply to discovery. Searching for a specific Teamarr
        event uses its original viewing options.
      </p>
      <div className="df-row df-wrap df-spacer">
        <button
          className="df-button"
          disabled={busy || pending}
          onClick={refresh}
        >
          Refresh available pages
        </button>
        <span>
          {inventory.last_success
            ? `Last refreshed ${new Date(inventory.last_success).toLocaleString()}`
            : "Pages have not been refreshed"}
        </span>
      </div>
      {pending && (
        <p role="status">
          {inventory.state === "refreshing"
            ? "Refreshing page tabs…"
            : "Refresh queued. It will run when automation owns an idle device."}
        </p>
      )}
      {inventory.error && (
        <p role="alert">
          {inventory.error}. Previous page inventory is retained.
        </p>
      )}
      {!pages.length && (
        <p>
          Refresh after adding or removing subscriptions, then enable the pages
          to use.
        </p>
      )}
      {pages.map((p) => (
        <div className="df-setting" key={p.id}>
          <div>
            <strong>{p.title || p.title_hint || p.id}</strong>
            {!p.available && <p>Currently unavailable</p>}
          </div>
          <input
            type="checkbox"
            aria-label={`Discover on ${p.title || p.title_hint || p.id}`}
            checked={settings.enabled_pages.includes(p.id)}
            disabled={
              busy ||
              (!settings.enabled_pages.includes(p.id) &&
                (!p.available || settings.enabled_pages.length >= 8))
            }
            onChange={(e) =>
              save({
                prime_discovery: {
                  ...settings,
                  enabled_pages: e.target.checked
                    ? [...settings.enabled_pages, p.id]
                    : settings.enabled_pages.filter((id) => id !== p.id),
                },
              })
            }
          />
        </div>
      ))}
      <p>
        {settings.enabled_pages.length
          ? `${settings.enabled_pages.length} pages enabled. New pages start disabled.`
          : "Discovery is off until a page is enabled."}
      </p>
      <label className="df-discovery-brief">
        <span>What would you like to watch?</span>
        <textarea
          aria-label="Live discovery interests"
          value={brief}
          maxLength={2000}
          disabled={busy}
          onChange={(e) => setBrief(e.target.value)}
          onBlur={() => {
            if (brief !== settings.brief)
              save({ prime_discovery: { ...settings, brief } });
          }}
        />
      </label>
      <label className="df-setting">
        <span>Tiles per page</span>
        <input
          type="number"
          min={1}
          max={100}
          aria-label="Discovery tiles per page"
          defaultValue={settings.limit_per_page}
          key={settings.limit_per_page}
          disabled={busy}
          onBlur={(e) => {
            const n = Number(e.target.value);
            if (
              Number.isInteger(n) &&
              n >= 1 &&
              n <= 100 &&
              n !== settings.limit_per_page
            )
              save({ prime_discovery: { ...settings, limit_per_page: n } });
            else e.target.value = String(settings.limit_per_page);
          }}
        />
      </label>
      <p>
        Discovery collects the first {settings.max_rows} rows of each enabled
        page. Current verified playback stays on while a refresh is queued.
      </p>
    </section>
  );
}

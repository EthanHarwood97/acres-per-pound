import type { Filters, Stats } from "../types";
import { fmtInt } from "../api";

interface Props {
  stats: Stats | null;
  total: number | null;
  onPreset: (p: Partial<Filters>) => void;
}

export function Header({ stats, total, onPreset }: Props) {
  const n = stats?.dwellings ?? stats?.n ?? 0;
  const withIn = stats?.dwellings_with_in ?? stats?.with_in ?? 0;
  const withEx = stats?.dwellings_with_ex ?? stats?.with_ex ?? 0;
  const pct = (v: number) => (n ? Math.round((v / n) * 100) : 0);

  return (
    <header className="masthead">
      <div className="masthead-left">
        <div className="wordmark">
          <span className="wordmark-acres">acres</span>
          <span className="wordmark-per">per</span>
          <span className="wordmark-pound">pound</span>
        </div>
        <p className="tagline">
          Every UK house and plot for sale, priced per square foot of land and interior.
        </p>
      </div>
      <div className="masthead-right">
        <div className="coverage">
          <div className="cov-item">
            <span className="cov-num">{fmtInt(total ?? n)}</span>
            <span className="cov-lbl">{total !== null ? "matching" : "listings"}</span>
          </div>
          <div className="cov-item" title="Interior floor area (EPC or portal-stated)">
            <span className="cov-num">{pct(withIn)}%</span>
            <span className="cov-lbl">interior</span>
          </div>
          <div className="cov-item" title="Registered plot boundary (HM Land Registry / RoS)">
            <span className="cov-num">{pct(withEx)}%</span>
            <span className="cov-lbl">land</span>
          </div>
          {stats?.last_seen && (
            <div className="cov-item">
              <span className="cov-num fresh">live</span>
              <span className="cov-lbl">
                {new Date(stats.last_seen).toLocaleDateString("en-GB", {
                  day: "numeric",
                  month: "short",
                })}
              </span>
            </div>
          )}
        </div>
        <div className="presets">
          <button className="preset" onClick={() => onPreset({ land_only: "1", sort: "gbp_ac", dir: "asc" })}>
            Bare land
          </button>
          <button className="preset" onClick={() => onPreset({ land_only: "0", has_ex: "1", sort: "gbp_ex", dir: "asc" })}>
            Houses with measured land
          </button>
          <button className="preset" onClick={() => onPreset({ has_in: "1", sort: "gbp_in", dir: "asc" })}>
            Cheapest per interior sq ft
          </button>
        </div>
      </div>
    </header>
  );
}

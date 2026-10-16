const labels: Record<string, string> = {
  "college-football": "College Football",
  pga: "Golf",
  tennis: "Tennis",
  "uefa.champions": "UEFA Champions League",
  "eng.2": "EFL Championship",
  "ger.1": "Bundesliga",
  "ger.2": "2. Bundesliga",
  f1: "Formula 1",
};

export const leagueName = (code: string) => labels[code] || code.toUpperCase();

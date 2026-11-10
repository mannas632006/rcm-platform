// A denial's title as rcm/work/denials.py writes it, with the record
// reference the exception manager appends. Every denial in the workbench
// otherwise reads as the same truncated sentence; leading with the codes lets
// the queue be scanned by reason. A title in any other shape shows as written.
const DENIAL_TITLE =
  /^Payer denied ([A-Z]{2}-[A-Z0-9]+(?:, [A-Z]{2}-[A-Z0-9]+)*): (.+?)(?: \([a-z_]+ [0-9a-f]+\))?$/;

export interface DenialTitle {
  codes: { group_code: string; reason_code: string }[];
  action: string;
}

export function parseDenialTitle(title: string): DenialTitle | null {
  const match = DENIAL_TITLE.exec(title);
  if (!match?.[1] || !match[2]) return null;
  return {
    codes: match[1].split(", ").map((code) => {
      const [group_code = "", reason_code = ""] = code.split("-");
      return { group_code, reason_code };
    }),
    action: match[2].charAt(0).toUpperCase() + match[2].slice(1),
  };
}

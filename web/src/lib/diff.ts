/** A line-by-line diff: what changed between Jarvis's draft and the version you'll send. */

export interface DiffLine {
  kind: "same" | "added" | "removed";
  text: string;
}

/** Longest-common-subsequence diff of two texts, line by line. */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.replace(/\r\n/g, "\n").split("\n");
  const b = after.replace(/\r\n/g, "\n").split("\n");
  const rows = a.length;
  const cols = b.length;
  // lengths[i][j]: the longest common run of a[i:] and b[j:]
  const lengths: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(cols + 1).fill(0),
  );
  for (let i = rows - 1; i >= 0; i--) {
    for (let j = cols - 1; j >= 0; j--) {
      lengths[i]![j] =
        a[i] === b[j]
          ? lengths[i + 1]![j + 1]! + 1
          : Math.max(lengths[i + 1]![j]!, lengths[i]![j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < rows && j < cols) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i]! });
      i++;
      j++;
    } else if (lengths[i + 1]![j]! >= lengths[i]![j + 1]!) {
      out.push({ kind: "removed", text: a[i]! });
      i++;
    } else {
      out.push({ kind: "added", text: b[j]! });
      j++;
    }
  }
  while (i < rows) out.push({ kind: "removed", text: a[i++]! });
  while (j < cols) out.push({ kind: "added", text: b[j++]! });
  return out;
}

export function changed(before: string | null | undefined, after: string): boolean {
  return before != null && before.trim() !== after.trim();
}

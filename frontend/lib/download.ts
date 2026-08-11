/**
 * Trigger a browser download of `content` as a file, without persisting it
 * server-side. The blob URL is revoked immediately after the download click
 * so it doesn't leak.
 */
export function downloadTextFile(
  filename: string,
  content: string,
  contentType = "text/plain;charset=utf-8",
): void {
  const blob = new Blob([content], { type: contentType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

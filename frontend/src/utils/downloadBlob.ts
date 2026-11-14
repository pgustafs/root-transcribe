/** Trigger a browser download for an in-memory blob.
 *
 * The one shared implementation of the createObjectURL → `<a download>` →
 * click → revoke idiom (previously copy-pasted per feature, which let fixes
 * drift): the anchor is appended to the DOM before clicking — programmatic
 * clicks on detached anchors are ignored by some Firefox/Safari versions —
 * and the object URL is always revoked.
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

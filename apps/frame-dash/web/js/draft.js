// One active editor owns the navigation/unload guard. No drafts in storage.
let guard = null;
let cleanup = () => {};
export function setDraftGuard(check) {
  cleanup();
  guard = check;
  const owner = window;
  const beforeUnload = (event) => {
    if (!guard?.()) return;
    event.preventDefault();
    event.returnValue = "";
  };
  owner.addEventListener("beforeunload", beforeUnload);
  cleanup = () => owner.removeEventListener("beforeunload", beforeUnload);
}
export function confirmLeaveDraft() {
  if (guard?.() && !confirm("Leave this playlist? Unsaved changes will be lost. If a request is in progress, wait for it to finish first.")) return false;
  cleanup();
  guard = null;
  return true;
}

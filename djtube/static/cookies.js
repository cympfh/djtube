export function cookiePanelOpen(decks) {
  return !!(decks?.A?.cookies || decks?.B?.cookies);
}

export function cookiePanelShown(failureOpen, chosenOpen) {
  return !!(failureOpen || chosenOpen);
}

export function nextChosenOpen(failureOpen, chosenOpen) {
  if (!cookiePanelShown(failureOpen, chosenOpen)) return true;
  if (failureOpen) return !!chosenOpen;
  return false;
}

export function bindCookies(prefix) {
  const panel = document.getElementById("cookie-panel");
  const opener = document.getElementById("cookie-open");
  const form = document.getElementById("cookie-form");
  const status = document.getElementById("cookie-status");
  const input = document.getElementById("cookie-file");
  const button = document.getElementById("cookie-upload");
  const pasteForm = document.getElementById("cookie-paste");
  const pasteText = document.getElementById("cookie-text");
  const pasteButton = document.getElementById("cookie-save");
  let failure = false;
  let heldOpen = false;

  function apply() {
    const open = cookiePanelShown(failure, heldOpen);
    if (panel) panel.hidden = !open;
    if (opener) opener.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function setOpen(open) {
    failure = !!open;
    apply();
  }

  if (opener) {
    opener.addEventListener("click", () => {
      const wasShown = cookiePanelShown(failure, heldOpen);
      heldOpen = nextChosenOpen(failure, heldOpen);
      apply();
      if (!wasShown && panel && !panel.hidden) panel.scrollIntoView({ block: "nearest" });
    });
  }

  apply();
  if (!form || !status || !input || !button) return { setOpen };

  function show(text, isError) {
    status.textContent = text;
    status.classList.toggle("is-error", !!isError);
  }

  function setBusy(busy) {
    button.disabled = busy;
    if (pasteButton) pasteButton.disabled = busy;
  }

  async function refresh() {
    try {
      const response = await fetch(`${prefix}/api/cookies`);
      if (!response.ok) {
        show("Cookie を確認できません", true);
        return;
      }
      const body = await response.json();
      show(body.present ? "アップロード済み。切れたら、ここで差し替えられます。" : "まだありません", false);
    } catch {
      show("Cookie を確認できません", true);
    }
  }

  async function save(payload, onSuccess) {
    setBusy(true);
    try {
      const response = await fetch(`${prefix}/api/cookies`, { method: "POST", body: payload });
      let detail = "";
      try {
        const body = await response.json();
        if (typeof body?.detail === "string") detail = body.detail;
        if (response.ok && body?.present) {
          show("アップロード済み。切れたら、ここで差し替えられます。", false);
          onSuccess();
          return;
        }
      } catch {
        detail = "";
      }
      show(detail || "Cookie を保存できませんでした", true);
    } catch {
      show("Cookie を保存できませんでした", true);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const chosen = input.files && input.files[0];
    if (!chosen) {
      show("Cookie のファイルを選んでください", true);
      return;
    }
    const payload = new FormData();
    payload.append("file", chosen);
    save(payload, () => form.reset());
  });

  if (pasteForm && pasteText && pasteButton) {
    pasteForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!pasteText.value.trim()) {
        show("Cookie を貼り付けてください", true);
        return;
      }
      const payload = new FormData();
      payload.append("text", pasteText.value);
      save(payload, () => {
        pasteText.value = "";
      });
    });
  }

  refresh();
  return { setOpen };
}

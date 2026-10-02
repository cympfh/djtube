export function cookiePanelOpen(decks) {
  return !!(decks?.A?.cookies || decks?.B?.cookies);
}

export function bindCookies(prefix) {
  const panel = document.getElementById("cookie-panel");
  const form = document.getElementById("cookie-form");
  const status = document.getElementById("cookie-status");
  const input = document.getElementById("cookie-file");
  const button = document.getElementById("cookie-upload");

  function setOpen(open) {
    if (panel) panel.hidden = !open;
  }

  setOpen(false);
  if (!form || !status || !input || !button) return { setOpen };

  function show(text, isError) {
    status.textContent = text;
    status.classList.toggle("is-error", !!isError);
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

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const chosen = input.files && input.files[0];
    if (!chosen) {
      show("Cookie のファイルを選んでください", true);
      return;
    }
    const payload = new FormData();
    payload.append("file", chosen);
    button.disabled = true;
    try {
      const response = await fetch(`${prefix}/api/cookies`, { method: "POST", body: payload });
      let detail = "";
      try {
        const body = await response.json();
        if (typeof body?.detail === "string") detail = body.detail;
        if (response.ok && body?.present) {
          show("アップロード済み。切れたら、ここで差し替えられます。", false);
          form.reset();
          return;
        }
      } catch {
        detail = "";
      }
      show(detail || "Cookie を保存できませんでした", true);
    } catch {
      show("Cookie を保存できませんでした", true);
    } finally {
      button.disabled = false;
    }
  });

  refresh();
  return { setOpen };
}

const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorEl.hidden = true;
  const data = new FormData(form);
  const response = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      username: data.get("username"),
      password: data.get("password"),
    }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: "登录失败" }));
    errorEl.textContent = payload.detail || "登录失败";
    errorEl.hidden = false;
    return;
  }
  window.location.href = "/";
});

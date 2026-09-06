/* Display-only USD formatting. Backend decimal strings remain authoritative. */
(function (root) {
  function formatCost(value) {
    if (value == null || !["string", "number", "bigint"].includes(typeof value)) return "—";
    const text = String(value).trim();
    if (!text || text.length > 1024) return "—";
    const match = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/.exec(text);
    if (!match) return "—";
    const fraction = match[3] ?? match[4] ?? "";
    const exponent = Number(match[5] || 0);
    if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 1000) return "—";
    const digits = ((match[2] || "0") + fraction).replace(/^0+/, "") || "0";
    if (digits === "0") return "$0.00";
    const negative = match[1] === "-";
    const scale = fraction.length - exponent;
    if (digits.length - scale <= -2) return negative ? ">−$0.01" : "<$0.01";
    const shift = 2 - scale;
    let cents;
    if (shift >= 0) cents = BigInt(digits) * (10n ** BigInt(shift));
    else {
      const divisor = 10n ** BigInt(-shift);
      cents = (BigInt(digits) + divisor / 2n) / divisor;
    }
    const whole = (cents / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return `${negative ? "−" : ""}$${whole}.${(cents % 100n).toString().padStart(2, "0")}`;
  }
  function usageWindow(usage) {
    for (const [key, label] of [["seven_day", "近 7 天"], ["today", "今日"], ["five_hour", "近 5 小时"]]) {
      const value = usage?.windows?.[key];
      if (value?.last_success_at) return { ...value, label, key };
    }
    return null;
  }
  const api = { formatCost, usageWindow };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Team48Format = api;
})(typeof window !== "undefined" ? window : globalThis);

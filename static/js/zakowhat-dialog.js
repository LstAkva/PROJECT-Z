/*
 * ZakoWhat dialogs — a lightweight, dependency-free replacement for window.alert/confirm/prompt.
 *
 *   await zwAlert(message, { variant, title, okText })            -> undefined
 *   await zwConfirm(message, { variant, title, confirmText, cancelText }) -> true | false
 *   await zwPrompt(message, defaultValue, { variant, title, confirmText, cancelText, placeholder }) -> string | null
 *
 * variant: "info" | "warning" | "danger" | "success". zwAlert picks one from the wording when omitted.
 * Built on the native <dialog> element (showModal): page behind is inert, focus is contained,
 * Escape cancels. Messages are rendered as text (never HTML). Dialogs are queued like native ones.
 */
(function () {
    "use strict";
    if (window.zwDialog) return;

    var STYLE_ID = "zw-dialog-style";
    var CSS = [
        ".zw-dialog{--zw-bg:#120c11;--zw-surface:#1a1016;--zw-text:#faf7f2;--zw-muted:#bdb4ae;--zw-border:rgba(197,160,89,.22);",
        "--zw-gold:#c5a059;--zw-gold-soft:rgba(197,160,89,.12);--zw-primary:#570f1c;--zw-primary-hover:#7a1627;",
        "--zw-danger:#b3263a;--zw-danger-hover:#c9334a;--zw-success:#7fae8a;--zw-warning:#d8b772;",
        "position:fixed;inset:0;margin:auto;height:fit-content;box-sizing:border-box;",
        "padding:0;border:1px solid var(--zw-border);border-radius:16px;background:linear-gradient(180deg,var(--zw-surface),var(--zw-bg));",
        "color:var(--zw-text);width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 48px);",
        "box-shadow:0 24px 64px -12px rgba(0,0,0,.75),0 0 0 1px rgba(0,0,0,.4);",
        "font-family:var(--font-sans,'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif);",
        "opacity:0;transform:translateY(8px) scale(.98);transition:opacity .18s ease,transform .18s cubic-bezier(.16,1,.3,1);}",
        ".zw-dialog[open].zw-in{opacity:1;transform:none;}",
        ".zw-dialog.zw-out{opacity:0;transform:translateY(6px) scale(.985);}",
        ".zw-dialog::backdrop{background:rgba(5,3,6,.68);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);}",
        ".zw-dialog__body{padding:26px 26px 6px;display:flex;gap:14px;align-items:flex-start;}",
        ".zw-dialog__icon{flex:0 0 36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;",
        "background:var(--zw-gold-soft);color:var(--zw-gold);font-size:18px;font-weight:700;line-height:1;}",
        ".zw-dialog[data-variant=danger] .zw-dialog__icon{background:rgba(179,38,58,.16);color:#e5707f;}",
        ".zw-dialog[data-variant=success] .zw-dialog__icon{background:rgba(127,174,138,.14);color:var(--zw-success);}",
        ".zw-dialog[data-variant=warning] .zw-dialog__icon{color:var(--zw-warning);}",
        ".zw-dialog__text{min-width:0;flex:1;}",
        ".zw-dialog__title{margin:4px 0 8px;font-family:var(--font-serif,'Playfair Display',Georgia,serif);font-size:19px;font-weight:600;letter-spacing:.01em;color:var(--zw-text);}",
        ".zw-dialog__message{margin:0;color:var(--zw-muted);font-size:15px;line-height:1.6;white-space:pre-line;overflow-wrap:anywhere;max-height:50vh;overflow:auto;}",
        ".zw-dialog__input{margin-top:14px;width:100%;box-sizing:border-box;padding:11px 13px;border-radius:10px;border:1px solid var(--zw-border);",
        "background:rgba(0,0,0,.35);color:var(--zw-text);font:inherit;font-size:15px;outline:none;}",
        ".zw-dialog__input:focus{border-color:var(--zw-gold);box-shadow:0 0 0 3px rgba(197,160,89,.25);}",
        ".zw-dialog__actions{display:flex;justify-content:flex-end;gap:10px;padding:20px 26px 24px;}",
        ".zw-dialog__btn{appearance:none;border-radius:10px;padding:10px 18px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;",
        "border:1px solid transparent;transition:background-color .15s ease,border-color .15s ease,color .15s ease;min-width:96px;}",
        ".zw-dialog__btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(197,160,89,.45);}",
        ".zw-dialog__btn--ghost{background:transparent;color:var(--zw-muted);border-color:var(--zw-border);}",
        ".zw-dialog__btn--ghost:hover{color:var(--zw-text);border-color:rgba(197,160,89,.45);}",
        ".zw-dialog__btn--primary{background:var(--zw-primary);color:#fff;border-color:rgba(197,160,89,.35);}",
        ".zw-dialog__btn--primary:hover{background:var(--zw-primary-hover);}",
        ".zw-dialog__btn--danger{background:var(--zw-danger);color:#fff;}",
        ".zw-dialog__btn--danger:hover{background:var(--zw-danger-hover);}",
        "[data-theme=light] .zw-dialog{--zw-bg:#fbf8f2;--zw-surface:#ffffff;--zw-text:#1d181a;--zw-muted:#54494d;--zw-border:rgba(110,85,40,.22);",
        "--zw-gold:#7a602b;--zw-gold-soft:rgba(122,96,43,.1);--zw-primary:#3b0a12;--zw-primary-hover:#570f1c;--zw-success:#3d7a4c;--zw-warning:#7a602b;",
        "box-shadow:0 24px 64px -16px rgba(40,20,10,.35);}",
        "[data-theme=light] .zw-dialog::backdrop{background:rgba(30,20,15,.45);}",
        "[data-theme=light] .zw-dialog__input{background:#fff;}",
        "@media (max-width:480px){.zw-dialog__body{padding:22px 20px 4px;}.zw-dialog__actions{padding:18px 20px 20px;flex-direction:column-reverse;}",
        ".zw-dialog__btn{width:100%;padding:12px 18px;}}",
        "@media (prefers-reduced-motion:reduce){.zw-dialog{transition:none;transform:none;}}"
    ].join("");

    var ICONS = { info: "i", warning: "!", danger: "!", success: "✓" };
    var TITLES = { info: "Maʼlumot", warning: "Diqqat", danger: "Xatolik", success: "Muvaffaqiyatli" };
    var queue = [];
    var active = false;

    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var style = document.createElement("style");
        style.id = STYLE_ID;
        style.textContent = CSS;
        (document.head || document.documentElement).appendChild(style);
    }

    /* Choose a visual variant from the (Uzbek) wording of a plain alert. */
    function guessVariant(message) {
        var m = String(message == null ? "" : message).toLowerCase();
        if (/^\s*✓|tabriklaymiz|muvaffaqiyatli/.test(m)) return "success";
        if (/xato|✕|bo'lmadi|boʻlmadi|topilmadi|rad etildi/.test(m)) return "danger";
        if (/iltimos|avval|bo'sh|mumkin emas|oshmasligi|allaqachon/.test(m)) return "warning";
        return "info";
    }

    /* "Xatolik: details" under a "Xatolik" title would repeat the word. */
    function displayMessage(message, variant) {
        var text = String(message == null ? "" : message);
        text = text.replace(/^\s*[✓✕]\s*/, "");  // the icon already shows ✓ / ✕
        if (variant === "danger") text = text.replace(/^\s*xatolik\s*:\s*/i, "");
        return text;
    }

    function build(opts) {
        var dialog = document.createElement("dialog");
        dialog.className = "zw-dialog";
        dialog.setAttribute("data-variant", opts.variant);
        dialog.setAttribute("aria-labelledby", "zw-dialog-title");
        dialog.setAttribute("aria-describedby", "zw-dialog-message");
        if (opts.kind !== "alert") dialog.setAttribute("role", "alertdialog");

        var body = document.createElement("div");
        body.className = "zw-dialog__body";
        var icon = document.createElement("div");
        icon.className = "zw-dialog__icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = ICONS[opts.variant] || ICONS.info;
        var textWrap = document.createElement("div");
        textWrap.className = "zw-dialog__text";
        var title = document.createElement("h2");
        title.className = "zw-dialog__title";
        title.id = "zw-dialog-title";
        title.textContent = opts.title;
        var msg = document.createElement("p");
        msg.className = "zw-dialog__message";
        msg.id = "zw-dialog-message";
        msg.textContent = opts.message;
        textWrap.appendChild(title);
        textWrap.appendChild(msg);

        var input = null;
        if (opts.kind === "prompt") {
            input = document.createElement("input");
            input.className = "zw-dialog__input";
            input.type = "text";
            input.value = opts.defaultValue == null ? "" : String(opts.defaultValue);
            input.setAttribute("aria-labelledby", "zw-dialog-message");
            input.autocomplete = "off";
            if (opts.placeholder) input.placeholder = opts.placeholder;
            textWrap.appendChild(input);
        }
        body.appendChild(icon);
        body.appendChild(textWrap);

        var actions = document.createElement("div");
        actions.className = "zw-dialog__actions";
        var cancelBtn = null;
        if (opts.kind !== "alert") {
            cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "zw-dialog__btn zw-dialog__btn--ghost";
            cancelBtn.textContent = opts.cancelText;
            actions.appendChild(cancelBtn);
        }
        var okBtn = document.createElement("button");
        okBtn.type = "button";
        okBtn.className = "zw-dialog__btn " + (opts.variant === "danger" && opts.kind !== "alert" ? "zw-dialog__btn--danger" : "zw-dialog__btn--primary");
        okBtn.textContent = opts.confirmText;
        actions.appendChild(okBtn);

        dialog.appendChild(body);
        dialog.appendChild(actions);
        return { dialog: dialog, okBtn: okBtn, cancelBtn: cancelBtn, input: input };
    }

    function open(opts) {
        ensureStyle();
        active = true;
        var previousFocus = document.activeElement;
        var parts = build(opts);
        var dialog = parts.dialog;
        var settled = false;

        return new Promise(function (resolve) {
            function finish(result) {
                if (settled) return;
                settled = true;
                dialog.classList.remove("zw-in");
                dialog.classList.add("zw-out");
                var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                setTimeout(function () {
                    try { dialog.close(); } catch (e) { /* already closed */ }
                    dialog.remove();
                    if (previousFocus && typeof previousFocus.focus === "function" && document.contains(previousFocus)) {
                        try { previousFocus.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
                    }
                    active = false;
                    resolve(result);
                    next();
                }, reduce ? 0 : 160);
            }

            var cancelValue = opts.kind === "confirm" ? false : (opts.kind === "prompt" ? null : undefined);
            function confirmValue() {
                if (opts.kind === "confirm") return true;
                if (opts.kind === "prompt") return parts.input.value;
                return undefined;
            }

            parts.okBtn.addEventListener("click", function () { finish(confirmValue()); });
            if (parts.cancelBtn) parts.cancelBtn.addEventListener("click", function () { finish(cancelValue); });
            // Escape: native "cancel" event — always resolves as Cancel (alert: just closes).
            dialog.addEventListener("cancel", function (e) { e.preventDefault(); finish(cancelValue); });
            // Enter submits only from the prompt field; elsewhere Enter activates the focused button
            // (destructive confirms focus Cancel, so Enter never confirms a destructive action by default).
            if (parts.input) {
                parts.input.addEventListener("keydown", function (e) {
                    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(confirmValue()); }
                });
            }
            // Clicks on the backdrop are ignored on purpose (no accidental dismiss or confirm).

            document.body.appendChild(dialog);
            if (typeof dialog.showModal === "function") {
                dialog.showModal();
            } else {
                dialog.setAttribute("open", "");
            }
            requestAnimationFrame(function () { dialog.classList.add("zw-in"); });

            var focusTarget = parts.input || (opts.variant === "danger" && parts.cancelBtn ? parts.cancelBtn : parts.okBtn);
            focusTarget.focus();
            if (parts.input) parts.input.select();
        });
    }

    function next() {
        if (active || !queue.length) return;
        var item = queue.shift();
        open(item.opts).then(item.resolve);
    }

    function enqueue(opts) {
        return new Promise(function (resolve) {
            queue.push({ opts: opts, resolve: resolve });
            if (document.body) {
                next();
            } else {
                document.addEventListener("DOMContentLoaded", next, { once: true });
            }
        });
    }

    function zwAlert(message, options) {
        var o = options || {};
        var variant = o.variant || guessVariant(message);
        return enqueue({
            kind: "alert",
            variant: variant,
            title: o.title || TITLES[variant],
            message: displayMessage(message, variant),
            confirmText: o.okText || "Tushunarli"
        });
    }

    function zwConfirm(message, options) {
        var o = options || {};
        var variant = o.variant || "warning";
        return enqueue({
            kind: "confirm",
            variant: variant,
            title: o.title || (variant === "danger" ? "Tasdiqlang" : "Tasdiqlash"),
            message: String(message == null ? "" : message),
            confirmText: o.confirmText || (variant === "danger" ? "Oʻchirish" : "Ha, davom etish"),
            cancelText: o.cancelText || "Bekor qilish"
        });
    }

    function zwPrompt(message, defaultValue, options) {
        var o = options || {};
        var variant = o.variant || "info";
        return enqueue({
            kind: "prompt",
            variant: variant,
            title: o.title || TITLES[variant],
            message: String(message == null ? "" : message),
            defaultValue: defaultValue,
            placeholder: o.placeholder || "",
            confirmText: o.confirmText || "Tasdiqlash",
            cancelText: o.cancelText || "Bekor qilish"
        });
    }

    window.zwDialog = { alert: zwAlert, confirm: zwConfirm, prompt: zwPrompt };
    window.zwAlert = zwAlert;
    window.zwConfirm = zwConfirm;
    window.zwPrompt = zwPrompt;
})();

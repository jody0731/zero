/* Attachments component web UI: composer upload controls and attachment cards. */

const SAFE_IMAGES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

function attachmentUrl(id, session, download = false) {
  const params = new URLSearchParams();
  if (session) params.set("session", session);
  if (download) params.set("download", "1");
  const query = params.toString();
  return `/api/attachments/${encodeURIComponent(id)}${query ? `?${query}` : ""}`;
}

export function register(host) {
  const { el, formatBytes } = host.dom;
  const uploadAttachment = host.api?.uploadAttachment;
  let openPicker = null;

  host.entry("attachments/file", (item, ctx = {}) => {
    const id = item?.attachment_id || "";
    const path = item?.path || "";
    if (!id && !path) return null;
    const session = ctx.session || host.dom.state?.open || null;
    const name = item.name || "file";
    const mediaType = name.toLowerCase().endsWith(".svg") ? "image/svg+xml"
      : String(item.media_type || "application/octet-stream")
        .split(";")[0].trim().toLowerCase();
    const sizeText = formatBytes && item.size_bytes ? formatBytes(item.size_bytes) : "";

    if (!id) {
      const card = el("div", "attachment-card");
      card.dataset.attachmentPath = path;
      const info = el("div", "attachment-card-info");
      info.append(el("span", "attachment-card-name", name));
      const meta = [mediaType, sizeText, path].filter(Boolean).join(" · ");
      if (meta) info.append(el("span", "attachment-card-meta", meta));
      card.append(info);
      return card;
    }

    const inlineUrl = attachmentUrl(id, session, false);
    const downloadUrl = attachmentUrl(id, session, true);

    const downloadLink = (label = "Download") => {
      const link = el("a", "attachment-download", label);
      link.href = downloadUrl;
      link.download = name;
      return link;
    };

    const card = el("div", "attachment-card");
    card.dataset.attachmentId = id;
    const info = el("div", "attachment-card-info");
    const description = (type) => [type, sizeText].filter(Boolean).join(" · ");
    const meta = el("span", "attachment-card-meta", description(mediaType));
    info.append(el("span", "attachment-card-name", name), meta);
    const download = downloadLink();
    card.append(info, download);

    const hasPreview = SAFE_IMAGES.has(mediaType) || mediaType === "application/pdf"
      || mediaType.startsWith("audio/") || mediaType.startsWith("video/");
    if (hasPreview) {
      // A saved entry may carry a raster hint for SVG. Ask the download door
      // for its vetted type before creating an inline element.
      fetch(inlineUrl, { method: "HEAD" }).then((response) => {
        if (!response.ok) return;
        const type = (response.headers.get("Content-Type") || "")
          .split(";")[0].trim().toLowerCase();
        if (type) meta.textContent = description(type);
        if (!response.headers.get("Content-Disposition")?.startsWith("inline;")) return;

        if (type === "application/pdf") {
          const openLink = el("a", "attachment-download", "Open");
          openLink.href = inlineUrl;
          openLink.target = "_blank";
          openLink.rel = "noopener noreferrer";
          download.before(openLink);
          return;
        }

        let preview;
        if (SAFE_IMAGES.has(type)) {
          preview = el("img", "attachment-image");
          preview.alt = name;
          preview.loading = "lazy";
        } else if (type.startsWith("audio/") || type.startsWith("video/")) {
          const tag = type.split("/")[0];
          preview = el(tag, `attachment-${tag}`);
          preview.controls = true;
          preview.preload = "metadata";
        } else {
          return;
        }
        preview.src = inlineUrl;
        const wrap = el("div", "attachment-media");
        wrap.dataset.attachmentId = id;
        const caption = el("div", "attachment-caption");
        caption.append(
          el("span", null, [name, sizeText].filter(Boolean).join(" · ")),
          downloadLink(),
        );
        wrap.append(preview, caption);
        card.replaceWith(wrap);
      }).catch(() => {
        // The download card remains when the preview metadata is unavailable.
      });
    }
    return card;
  });

  host.composer((ctx) => {
    const { form, textarea, controls, addAttachment, addWarning, removeWarning } = ctx;
    if (!controls || !form || !textarea) return;

    const fileInput = el("input", "attachment-file-input");
    fileInput.type = "file";
    fileInput.multiple = true;
    fileInput.hidden = true;
    fileInput.setAttribute("aria-label", "Choose files to attach");

    const button = el("button", "ghost attach-button", "Attach");
    button.type = "button";
    button.setAttribute("aria-label", "Attach file");
    button.addEventListener("click", () => fileInput.click());
    controls.append(button, fileInput);
    openPicker = () => fileInput.click();

    async function stageFiles(fileList) {
      const files = Array.from(fileList || []).filter(Boolean);
      if (!files.length || !uploadAttachment) return;
      const targetKey = ctx.currentDraftKey();
      for (const file of files) {
        try {
          const uploaded = await uploadAttachment(file);
          addAttachment(targetKey, uploaded);
        } catch (error) {
          if (error.overLimit) {
            const warn = el("div", "attachment-warning");
            warn.append(
              el("span", null, `${file.name || "File"} exceeds limit`),
              el("span", "spacer"),
            );
            const anyway = el("button", "attach-anyway", "Attach anyway");
            anyway.type = "button";
            const dismiss = el("button", "ghost", "Dismiss");
            dismiss.type = "button";
            anyway.addEventListener("click", async () => {
              anyway.disabled = true;
              try {
                const uploaded = await uploadAttachment(file, { allowOverLimit: true });
                removeWarning(targetKey, warn);
                addAttachment(targetKey, uploaded);
              } catch (retryError) {
                anyway.disabled = false;
                alert(retryError.message);
              }
            });
            dismiss.addEventListener("click", () => removeWarning(targetKey, warn));
            warn.append(anyway, dismiss);
            addWarning(targetKey, warn);
          } else {
            alert(error.message);
          }
        }
      }
    }

    fileInput.addEventListener("change", () => {
      const picked = Array.from(fileInput.files || []);
      fileInput.value = "";
      stageFiles(picked);
    });

    form.addEventListener("dragover", (event) => {
      if (!event.dataTransfer?.types?.includes("Files")) return;
      event.preventDefault();
      form.classList.add("drag-over");
    });

    form.addEventListener("dragleave", (event) => {
      if (!form.contains(event.relatedTarget)) form.classList.remove("drag-over");
    });

    form.addEventListener("drop", (event) => {
      form.classList.remove("drag-over");
      const files = event.dataTransfer?.files;
      if (!files || !files.length) return;
      event.preventDefault();
      stageFiles(files);
    });

    textarea.addEventListener("paste", (event) => {
      const files = Array.from(event.clipboardData?.files || []);
      if (!files.length) return;
      event.preventDefault();
      stageFiles(files);
    });
  });

  host.command("/attach", async () => {
    if (openPicker) openPicker();
    return true;
  });
}

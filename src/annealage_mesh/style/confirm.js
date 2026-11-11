// confirmRow(anchor, opts) -> Promise<boolean>: the page's own confirmation,
// in place of the browser's confirm(). It opens a .confirm row (theme.css)
// next to the control that asked and resolves true when the action is chosen,
// false for Cancel, Escape, another row opening, or the anchor leaving the
// page. Text is only ever set with textContent, so a title or a field value
// containing markup is shown literally.
//
//   const ok = await confirmRow(button, {
//     title: "Delete 3 pins",              // the action as a verb and an object
//     detail: "They are removed from the model.",   // optional, one line
//     fields: [["Model", "claude-sonnet"], ["Limit", "$5"]],   // optional, for a spend
//     action: "Delete",                    // the confirming button's label
//     danger: true,                        // destructive: danger text, focus on Cancel
//     cancel: "Cancel",                    // optional label
//     extra: node,                         // optional DOM node or array: warnings, a Details
//                                          // disclosure; sits between the detail and the actions
//     into: panelElement,                  // optional: append the row here
//   });
//
// Placement: into `into` when given; else in a .dt-detail under the anchor's
// .dt-row; else directly after the anchor. While open, the anchor is disabled
// so it cannot open a second row, and focus returns to it on close. One row is
// open per page: opening another resolves the first as false. The row is
// role=group with aria-live=polite, not a modal. It cannot guard
// beforeunload; that stays with the browser's own prompt.

// A detail longer than this goes behind a disclosure instead of a line.
const LONG_DETAIL = 120;

let current = null;
let serial = 0;

export function confirmRow(anchor, { title, detail, fields, action, danger = false, cancel = "Cancel", extra, into } = {}) {
  if (current) current.close(false);
  const doc = anchor.ownerDocument;
  const make = (tag, cls, text) => {
    const node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const id = `confirm-title-${++serial}`;
  const row = make("div", "confirm");
  row.setAttribute("data-kind", danger ? "danger" : "spend");
  row.setAttribute("role", "group");
  row.setAttribute("aria-labelledby", id);
  row.setAttribute("aria-live", "polite");

  let holder = row;
  let openRow = null;
  const detailRow = !into && anchor.closest(".dt-row");
  if (into) {
    into.appendChild(row);
  } else if (detailRow) {
    holder = make("div", "dt-detail");
    holder.appendChild(row);
    detailRow.parentNode.insertBefore(holder, detailRow.nextSibling);
    if (!detailRow.classList.contains("is-open")) {
      detailRow.classList.add("is-open");
      openRow = detailRow;
    }
  } else {
    anchor.parentNode.insertBefore(row, anchor.nextSibling);
  }

  const heading = make("p", "ch", title);
  heading.id = id;
  row.appendChild(heading);
  if (detail) {
    if (detail.length > LONG_DETAIL) {
      const disc = make("details", "disc");
      disc.appendChild(make("summary", "", "What this does"));
      const body = make("div", "disc-body");
      body.appendChild(make("p", "", detail));
      disc.appendChild(body);
      row.appendChild(disc);
    } else {
      row.appendChild(make("p", "cd", detail));
    }
  }
  if (fields && fields.length) {
    const list = make("dl", "fl");
    for (const [label, value] of fields) {
      list.appendChild(make("dt", "", label));
      list.appendChild(make("dd", "", value));
    }
    row.appendChild(list);
  }
  const extras = extra ? (Array.isArray(extra) ? extra : [extra]) : [];
  if (extras.length) {
    const slot = make("div", "cx");
    for (const node of extras) slot.appendChild(node);
    row.appendChild(slot);
  }
  const cancelButton = make("button", "btn quiet", cancel);
  cancelButton.setAttribute("type", "button");
  const actionButton = make("button", danger ? "btn danger" : "btn primary", action);
  actionButton.setAttribute("type", "button");
  const buttons = make("div", "cacts");
  buttons.appendChild(cancelButton);
  buttons.appendChild(actionButton);
  row.appendChild(buttons);

  const canDisable = "disabled" in anchor;
  const wasDisabled = canDisable ? anchor.disabled : anchor.getAttribute("aria-disabled");
  if (canDisable) anchor.disabled = true;
  else anchor.setAttribute("aria-disabled", "true");

  return new Promise((resolve) => {
    let done = false;
    const Observer = globalThis.MutationObserver;
    const observer = Observer ? new Observer(() => {
      if (!anchor.isConnected || !row.isConnected) close(false);
    }) : null;

    function close(result) {
      if (done) return;
      done = true;
      if (observer) observer.disconnect();
      doc.removeEventListener("keydown", onKey);
      if (current && current.close === close) current = null;
      const stay = anchor.isConnected;
      holder.remove();
      if (openRow) openRow.classList.remove("is-open");
      if (canDisable) anchor.disabled = wasDisabled;
      else if (wasDisabled === null) anchor.removeAttribute("aria-disabled");
      else anchor.setAttribute("aria-disabled", wasDisabled);
      if (stay) anchor.focus();
      resolve(result);
    }

    function onKey(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      close(false);
    }

    cancelButton.addEventListener("click", () => close(false));
    actionButton.addEventListener("click", () => close(true));
    doc.addEventListener("keydown", onKey);
    if (observer) observer.observe(doc.documentElement, { childList: true, subtree: true });
    current = { close };
    (danger ? cancelButton : actionButton).focus();
  });
}

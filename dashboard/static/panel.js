const keywords = document.querySelector("#keywords");
const template = document.querySelector("#keyword-template");
const addKeyword = document.querySelector("#add-keyword");

function applyHintType(row) {
  const brand = row.querySelector("[data-brand]");
  const needsBrand = row.querySelector("select").value === "brand";
  brand.hidden = !needsBrand;
  brand.querySelector("input").required = needsBrand;
}

if (keywords) {
  for (const row of keywords.querySelectorAll("[data-keyword]")) {
    applyHintType(row);
  }
  const maxKeywords = 100;
  addKeyword.disabled = keywords.children.length >= maxKeywords;

  keywords.addEventListener("change", (event) => {
    if (event.target.name === "hint_type") {
      applyHintType(event.target.closest("[data-keyword]"));
    }
  });

  keywords.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove]");
    if (remove) {
      const row = remove.closest("[data-keyword]");
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      addKeyword.disabled = false;
      (next?.querySelector("input") || addKeyword).focus();
    }
  });

  addKeyword.addEventListener("click", () => {
    const fragment = template.content.cloneNode(true);
    const row = fragment.querySelector("[data-keyword]");
    applyHintType(row);
    keywords.append(fragment);
    row.querySelector("input").focus();
    addKeyword.disabled = keywords.children.length >= maxKeywords;
  });
}

if (document.querySelector("[data-poll]")) {
  window.setTimeout(() => window.location.reload(), 3000);
}

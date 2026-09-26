import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Search } from "lucide-react";

type Option = { value: string; label: string };

function useAnchoredMenu() {
  const anchorRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [rect, setRect] = useState({
    left: 0,
    top: 0,
    width: 0,
    maxHeight: 280,
  });

  useLayoutEffect(() => {
    if (!open) return;
    function update() {
      const anchor = anchorRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const below = window.innerHeight - anchor.bottom - 12;
      const above = anchor.top - 12;
      const maxHeight = Math.min(300, Math.max(below, above));
      const placeAbove = below < 180 && above > below;
      setRect({
        left: Math.max(
          8,
          Math.min(anchor.left, window.innerWidth - anchor.width - 8),
        ),
        top: placeAbove
          ? Math.max(8, anchor.top - maxHeight - 4)
          : anchor.bottom + 4,
        width: anchor.width,
        maxHeight,
      });
    }
    function closeOutside(event: PointerEvent) {
      const target = event.target as Node;
      if (
        !anchorRef.current?.contains(target) &&
        !menuRef.current?.contains(target)
      )
        setOpen(false);
    }
    function closeEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        anchorRef.current?.focus();
      }
    }
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
    };
  }, [open]);

  return { anchorRef, menuRef, open, setOpen, rect };
}

export function AgentSelectField({
  label,
  options,
  value,
  onChange,
  placeholder,
  disabled = false,
  helper,
}: {
  label: string;
  options: Option[];
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  disabled?: boolean;
  helper?: string;
}) {
  const menu = useAnchoredMenu();
  const selected = options.find((option) => option.value === value);
  useEffect(() => {
    if (menu.open)
      menu.menuRef.current
        ?.querySelector<HTMLButtonElement>(
          '[aria-selected="true"], .agent-select-option',
        )
        ?.focus();
  }, [menu.open]);
  return (
    <div className="agent-select-field">
      <span className="config-label">{label}</span>
      <button
        ref={menu.anchorRef}
        type="button"
        className="agent-select-trigger"
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={menu.open}
        disabled={disabled}
        onClick={() => menu.setOpen((previous) => !previous)}
      >
        <span className={selected ? "" : "agent-select-placeholder"}>
          {selected?.label ?? placeholder}
        </span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {helper && <small className="config-help">{helper}</small>}
      {menu.open &&
        createPortal(
          <div
            ref={menu.menuRef}
            className="agent-select-menu"
            role="listbox"
            aria-label={label}
            onKeyDown={(event) => {
              if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
              event.preventDefault();
              const buttons = [
                ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
                  ".agent-select-option",
                ),
              ];
              const index = buttons.indexOf(
                document.activeElement as HTMLButtonElement,
              );
              buttons[
                (index +
                  (event.key === "ArrowDown" ? 1 : -1) +
                  buttons.length) %
                  buttons.length
              ]?.focus();
            }}
            style={{
              left: menu.rect.left,
              top: menu.rect.top,
              width: menu.rect.width,
              maxHeight: menu.rect.maxHeight,
            }}
          >
            {options.map((option) => (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={option.value === value}
                className="agent-select-option"
                onClick={() => {
                  onChange(option.value);
                  menu.setOpen(false);
                  menu.anchorRef.current?.focus();
                }}
              >
                <span>{option.label}</span>
                {option.value === value && (
                  <Check size={15} aria-hidden="true" />
                )}
              </button>
            ))}
            {!options.length && (
              <p className="agent-select-empty">No options available</p>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}

export function AgentMultiSelectField({
  label,
  options,
  value,
  onChange,
  empty,
  placeholder = "Choose one or more",
  disabled = false,
}: {
  label: string;
  options: string[];
  value: string[];
  onChange: (value: string[]) => void;
  empty: string;
  placeholder?: string;
  disabled?: boolean;
}) {
  const menu = useAnchoredMenu();
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (menu.open) searchRef.current?.focus();
    else setQuery("");
  }, [menu.open]);
  const available = [...new Set([...options, ...value])];
  const filtered = available.filter((option) =>
    option.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const chosen = value.slice(0, 2);
  return (
    <div className="agent-select-field">
      <span className="config-label">{label}</span>
      <button
        ref={menu.anchorRef}
        type="button"
        className="agent-select-trigger"
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={menu.open}
        disabled={disabled}
        onClick={() => menu.setOpen((previous) => !previous)}
      >
        <span className="agent-select-values">
          {!value.length ? (
            <span className="agent-select-placeholder">{placeholder}</span>
          ) : (
            <>
              {chosen.map((item) => (
                <span className="agent-select-chip" key={item}>
                  {item}
                </span>
              ))}
              {value.length > 2 && (
                <span className="agent-select-chip">+{value.length - 2}</span>
              )}
            </>
          )}
        </span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {!options.length && empty && (
        <small className="config-help">{empty}</small>
      )}
      {menu.open &&
        createPortal(
          <div
            ref={menu.menuRef}
            className="agent-select-menu agent-multi-menu"
            style={{
              left: menu.rect.left,
              top: menu.rect.top,
              width: menu.rect.width,
              maxHeight: menu.rect.maxHeight,
            }}
          >
            <div className="agent-select-search">
              <Search size={14} aria-hidden="true" />
              <input
                ref={searchRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search options..."
                aria-label={`Search ${label}`}
              />
            </div>
            <div className="agent-multi-actions">
              <button
                type="button"
                onClick={() => onChange([...new Set([...value, ...options])])}
              >
                Select all
              </button>
              <button type="button" onClick={() => onChange([])}>
                Clear
              </button>
            </div>
            <div
              className="agent-select-options"
              role="listbox"
              aria-label={label}
              aria-multiselectable="true"
            >
              {filtered.map((option) => (
                <button
                  key={option}
                  type="button"
                  role="option"
                  aria-selected={value.includes(option)}
                  className="agent-select-option"
                  onClick={() =>
                    onChange(
                      value.includes(option)
                        ? value.filter((item) => item !== option)
                        : [...value, option],
                    )
                  }
                >
                  <span className="agent-option-label">{option}</span>
                  <span
                    className={`agent-option-check ${value.includes(option) ? "checked" : ""}`}
                  >
                    {value.includes(option) && (
                      <Check size={12} aria-hidden="true" />
                    )}
                  </span>
                </button>
              ))}
              {!filtered.length && (
                <p className="agent-select-empty">
                  {query ? "No matching options" : empty}
                </p>
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

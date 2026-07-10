"use client";

import { CheckIcon, MonitorIcon, MoonIcon, SunIcon } from "lucide-react";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { type Theme, useTheme } from "@/components/theme-provider";
import { cn } from "@/lib/utils";

const THEME_OPTIONS: { value: Theme; label: string; icon: typeof SunIcon }[] = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
  { value: "system", label: "System", icon: MonitorIcon },
];

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const ActiveIcon =
    THEME_OPTIONS.find((option) => option.value === theme)?.icon ?? MonitorIcon;

  return (
    <Popover>
      <PopoverTrigger
        aria-label="Toggle theme"
        className="inline-flex size-7 shrink-0 items-center justify-center rounded-[min(var(--radius-md),12px)] text-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        type="button"
      >
        <ActiveIcon className="size-3.5" />
      </PopoverTrigger>
      <PopoverContent align="end" className="w-36 p-1.5" side="bottom">
        <div className="grid gap-0.5">
          {THEME_OPTIONS.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              className={cn(
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted",
                theme === value && "bg-muted",
              )}
              onClick={() => setTheme(value)}
              type="button"
            >
              <Icon className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="flex-1">{label}</span>
              {theme === value ? (
                <CheckIcon className="size-3.5 shrink-0 text-muted-foreground" />
              ) : null}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

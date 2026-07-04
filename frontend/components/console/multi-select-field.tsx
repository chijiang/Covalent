"use client";

import { useMemo, useState } from "react";
import { ChevronDown, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

export type MultiSelectOption = {
  value: string;
  label: string;
  hint?: string;
};

type MultiSelectFieldProps = {
  label: string;
  helper?: string;
  options: MultiSelectOption[];
  value: string[];
  onChange: (nextValue: string[]) => void;
  placeholder?: string;
  noOptionsMessage?: string | ((inputValue: string) => string);
  isDisabled?: boolean;
};

function dedupeStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

export function MultiSelectField({
  label,
  helper,
  options,
  value,
  onChange,
  placeholder = "Choose one or more",
  noOptionsMessage = "No matching options",
  isDisabled = false,
}: MultiSelectFieldProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const optionByValue = useMemo(() => new Map(options.map((option) => [option.value, option])), [options]);

  const filteredOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) {
      return options;
    }
    return options.filter((option) => `${option.label} ${option.hint || ""}`.toLowerCase().includes(normalizedQuery));
  }, [options, query]);

  const emptyMessage =
    typeof noOptionsMessage === "function" ? noOptionsMessage(query) : noOptionsMessage;

  function toggleOption(optionValue: string) {
    onChange(
      value.includes(optionValue)
        ? value.filter((item) => item !== optionValue)
        : dedupeStrings([...value, optionValue]),
    );
  }

  function removeOption(optionValue: string, event?: React.SyntheticEvent) {
    event?.preventDefault();
    event?.stopPropagation();
    onChange(value.filter((item) => item !== optionValue));
  }

  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Popover onOpenChange={setOpen} open={open}>
        <PopoverTrigger
          className={cn(
            "flex min-h-10 w-full items-center justify-between gap-2 rounded-lg border border-input bg-background px-3 py-2 text-left text-sm shadow-xs transition-colors",
            "hover:bg-muted/30 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
            isDisabled && "cursor-not-allowed opacity-50",
          )}
          disabled={isDisabled}
          type="button"
        >
          <div className="flex min-w-0 flex-1 flex-wrap gap-1.5">
            {value.length === 0 ? (
              <span className="text-muted-foreground">{placeholder}</span>
            ) : (
              value.map((item) => {
                const option = optionByValue.get(item);
                return (
                  <Badge className="gap-1 pr-1" key={item} variant="secondary">
                    {option?.label ?? item}
                    <span
                      aria-label={`Remove ${option?.label ?? item}`}
                      aria-hidden={isDisabled}
                      className={cn(
                        "rounded-sm p-0.5 hover:bg-muted",
                        isDisabled && "pointer-events-none",
                      )}
                      onClick={(event) => {
                        if (isDisabled) {
                          return;
                        }
                        removeOption(item, event);
                      }}
                      onKeyDown={(event) => {
                        if (isDisabled) {
                          return;
                        }
                        if (event.key === "Enter" || event.key === " ") {
                          removeOption(item, event);
                        }
                      }}
                      onPointerDown={(event) => event.stopPropagation()}
                      role="button"
                      tabIndex={isDisabled ? -1 : 0}
                    >
                      <X className="size-3" />
                    </span>
                  </Badge>
                );
              })
            )}
          </div>
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[min(100vw-2rem,28rem)] gap-0 p-0">
          <div className="border-b border-border p-2">
            <Input
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search options..."
              value={query}
            />
          </div>
          <ScrollArea className="max-h-60">
            <div className="p-1">
              {filteredOptions.length === 0 ? (
                <p className="px-3 py-6 text-center text-sm text-muted-foreground">{emptyMessage}</p>
              ) : (
                filteredOptions.map((option) => {
                  const checked = value.includes(option.value);
                  return (
                    <button
                      className={cn(
                        "flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors hover:bg-muted/60",
                        checked && "bg-muted/40",
                      )}
                      key={option.value}
                      onClick={() => toggleOption(option.value)}
                      type="button"
                    >
                      <Checkbox checked={checked} className="pointer-events-none mt-0.5" />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-foreground">{option.label}</span>
                        {option.hint ? <span className="mt-0.5 block text-xs text-muted-foreground">{option.hint}</span> : null}
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          </ScrollArea>
          {value.length > 0 ? (
            <div className="border-t border-border p-2">
              <Button className="w-full" onClick={() => onChange([])} size="sm" type="button" variant="ghost">
                Clear selection
              </Button>
            </div>
          ) : null}
        </PopoverContent>
      </Popover>
      {helper ? <p className="text-[13px] leading-relaxed text-muted-foreground">{helper}</p> : null}
    </div>
  );
}

import { cn } from "@/lib/utils";

type FilterToggleGroupProps<T extends string> = {
  value: T;
  onChange: (value: T) => void;
  options: readonly (readonly [T, string])[];
};

export function FilterToggleGroup<T extends string>({ value, onChange, options }: FilterToggleGroupProps<T>) {
  return (
    <div className="console-filter-group">
      {options.map(([optionValue, label]) => (
        <button
          className={cn("console-filter-chip", value === optionValue && "is-active")}
          key={optionValue}
          onClick={() => onChange(optionValue)}
          type="button"
        >
          {label}
        </button>
      ))}
    </div>
  );
}

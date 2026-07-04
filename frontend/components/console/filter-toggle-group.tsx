import { Button } from "@/components/ui/button";

type FilterToggleGroupProps<T extends string> = {
  value: T;
  onChange: (value: T) => void;
  options: readonly (readonly [T, string])[];
};

export function FilterToggleGroup<T extends string>({ value, onChange, options }: FilterToggleGroupProps<T>) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {options.map(([optionValue, label]) => (
        <Button
          key={optionValue}
          onClick={() => onChange(optionValue)}
          size="xs"
          type="button"
          variant={value === optionValue ? "default" : "outline"}
        >
          {label}
        </Button>
      ))}
    </div>
  );
}

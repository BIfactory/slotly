/**
 * Small Tailwind primitives used by the auth pages. Replace with shadcn/ui later.
 */
"use client";

import { ChevronDown } from "lucide-react";
import { type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";

type ButtonVariant = "primary" | "secondary";

export function Button({
  variant = "primary",
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  // Mobile-first: 44px tap target (WCAG 2.5.5 minimum). Desktop keeps the
  // tighter 40px so dense forms don't look chunky with a mouse.
  const base =
    "inline-flex h-11 w-full items-center justify-center rounded-md px-4 text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 sm:h-10";
  const styles: Record<ButtonVariant, string> = {
    primary:
      "bg-indigo-600 text-white shadow-sm hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 dark:bg-indigo-500 dark:hover:bg-indigo-600 dark:focus-visible:ring-offset-zinc-900",
    secondary:
      "border border-zinc-200 bg-white text-zinc-900 hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-50 dark:hover:bg-zinc-900",
  };
  return <button {...rest} className={`${base} ${styles[variant]} ${className}`} />;
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={
        "flex h-10 w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-950 dark:placeholder:text-zinc-500 dark:focus:ring-indigo-400 " +
        (props.className ?? "")
      }
    />
  );
}

/** Native <select> wrapped with a custom chevron so the dropdown indicator
 * sits at right-3 (with breathing room from the border) instead of being
 * jammed against the edge by the browser's default rendering. */
export function Select({
  className = "",
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className="relative">
      <select
        {...rest}
        className={
          "h-10 w-full appearance-none rounded-md border border-zinc-200 bg-white pl-3 pr-9 text-sm dark:border-zinc-800 dark:bg-zinc-950 " +
          className
        }
      >
        {children}
      </select>
      <ChevronDown
        size={16}
        aria-hidden
        className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-zinc-500 dark:text-zinc-400"
      />
    </div>
  );
}

export function Label({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) {
  return (
    <label htmlFor={htmlFor} className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
      {children}
    </label>
  );
}

export function FormError({ message }: { message?: string | null }) {
  if (!message) return null;
  return (
    <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
      {message}
    </p>
  );
}

export function FormSuccess({ message }: { message?: string | null }) {
  if (!message) return null;
  return (
    <p className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
      {message}
    </p>
  );
}

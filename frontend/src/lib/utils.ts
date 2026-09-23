import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** shadcn-vue 约定的 class 合并工具。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

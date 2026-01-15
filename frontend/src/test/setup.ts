/**
 * Vitest setup file - configures jest-dom matchers.
 * MSW is set up per-test-file to avoid conflicts with other mock strategies.
 */
import { expect } from 'vitest'
import * as matchers from '@testing-library/jest-dom/matchers'

// Extend vitest's expect with jest-dom matchers
expect.extend(matchers)

// Mock ResizeObserver for libraries like recharts that require it
// ResizeObserver is not available in jsdom by default
global.ResizeObserver = class ResizeObserver {
  observe() {
    // Mock implementation - no-op
  }
  unobserve() {
    // Mock implementation - no-op
  }
  disconnect() {
    // Mock implementation - no-op
  }
} as typeof ResizeObserver

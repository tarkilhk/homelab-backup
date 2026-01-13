/**
 * Vitest setup file - configures jest-dom matchers.
 * MSW is set up per-test-file to avoid conflicts with other mock strategies.
 */
import { expect } from 'vitest'
import * as matchers from '@testing-library/jest-dom/matchers'

// Extend vitest's expect with jest-dom matchers
expect.extend(matchers)

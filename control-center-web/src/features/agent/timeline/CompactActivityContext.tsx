import { createContext } from 'react';

/** Embedded work surfaces can opt into a quieter process history. */
export const CompactActivityContext = createContext(false);

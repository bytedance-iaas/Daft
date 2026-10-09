// Opening the mini player on a moment that is not a finding (design doc 22 §3.3): a sample frame of a
// camera of the task's EEF bundle, as the opinion's evidence frames cite it.
import { createContext } from 'react';

/** A camera of the EEF bundle (its own id, as the opinion names it) and a sample frame of it. */
export interface MiniSeek {
  camera: string;
  frame: number;
}

/** Given by a page that can open the mini player (the report's Episode 明细); null where none can. */
export const MiniPlayerOpen = createContext<((seek: MiniSeek) => void) | null>(null);

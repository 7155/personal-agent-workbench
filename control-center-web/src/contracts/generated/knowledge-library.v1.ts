/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/knowledge-library.v1.json
 */

export interface KnowledgeLibraryV1 {
  schemaVersion: 'rag-ime.knowledge-library.v1';
  bases?: {
    id: string;
    name: string;
    description?: string;
    parserMode: 'auto' | 'builtin' | 'mineru';
    agentEnabled: boolean;
    chunkingConfig?: ChunkingConfig;
    retrievalConfig?: RetrievalConfig;
    configRevision?: number;
    [k: string]: unknown;
  }[];
  items?: unknown[];
  [k: string]: unknown;
}
export interface ChunkingConfig {
  strategy: 'general' | 'markdown' | 'paper' | 'book' | 'qa' | 'laws' | 'separator' | 'fixed';
  size: number;
  overlap: number;
  separator: string;
  respectHeadings: boolean;
  respectPageBoundaries: boolean;
}
export interface RetrievalConfig {
  mode: 'lexical' | 'hybrid' | 'dense';
  topK: number;
  threshold: number;
  lexicalWeight: number;
  denseWeight: number;
  rrfK: number;
  candidateMultiplier: number;
}

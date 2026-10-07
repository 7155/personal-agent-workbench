import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MotionActivityBoundary } from '@/design/motion';
import { KnowledgeMaterialsPanel } from './document-workspace';
import type { KnowledgeDocument } from './api';
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }));
afterEach(cleanup);
it('gates pipeline feedback on host activity without changing supplied document progress or outcome', () => {
  const document = { id:'test-document',baseId:'test-base',name:'材料.md',status:'parsing',stage:'parsing',progress:.45,error:'',chunkCount:0,parser:'builtin',byteSize:12,updatedAtMs:1,mimeType:'text/markdown' } as KnowledgeDocument;
  const view = (active:boolean,ready=false) => <MotionActivityBoundary active={active}><KnowledgeMaterialsPanel
    documents={[{...document,status:ready?'ready':'parsing',stage:ready?'ready':'parsing',progress:ready?1:.45}]} detail={null} detailError={null} detailLoading={false}
    dropSupported={false} error={null} loading={false} filter="" onFilterChange={()=>{}} importError={null} importing={false}
    onDelete={()=>{}} onImport={()=>{}} onImportFiles={()=>{}} onOpen={()=>{}} onReparse={()=>{}} onRetryList={()=>{}} onRetryDetail={()=>{}} onRetryUpload={()=>{}} onClearUploads={()=>{}} onSelect={()=>{}} pendingDocumentId="" selectedDocumentId={document.id} uploadItems={[]}/></MotionActivityBoundary>;
  const result = render(view(true));const pipeline = () => result.container.querySelector('.knowledge-pipeline')!;
  expect(pipeline()).toHaveAttribute('data-motion-active','true');expect(screen.getByText('45%')).toBeInTheDocument();
  result.rerender(view(false));expect(pipeline()).toHaveAttribute('data-motion-active','false');expect(pipeline()).toHaveAttribute('data-status','parsing');expect(screen.getByText('45%')).toBeInTheDocument();
  result.rerender(view(false,true));expect(pipeline()).toHaveAttribute('data-status','ready');expect(pipeline().querySelector('[data-state="active"]')).toBe(null);
  result.rerender(view(true,true));expect(pipeline().querySelector('[data-state="active"]')).toBe(null);expect(screen.getByText('解析与索引已完成，这份材料可以检索。')).toBeInTheDocument();
});

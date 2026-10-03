import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import desktopSource from '../shell/PawDesktop.tsx?raw';
import appLoaderSource from '../apps/PawApps.tsx?raw';
import appRuntimeSource from '../apps/PawAppsRuntime.tsx?raw';
import appRegistrySource from './app-registry.ts?raw';
import filesSource from '../../features/files/PawOsFilesApp.tsx?raw';
import terminalSource from '../../features/terminal/PawOsTerminalApp.tsx?raw';

const visibleSurfaceFiles: Array<[string, string]> = [
  ['paw-os/shell/PawDesktop.tsx', desktopSource],
  ['paw-os/apps/PawApps.tsx', appLoaderSource],
  ['paw-os/apps/PawAppsRuntime.tsx', appRuntimeSource],
  ['paw-os/runtime/app-registry.ts', appRegistrySource],
  ['features/files/PawOsFilesApp.tsx', filesSource],
  ['features/terminal/PawOsTerminalApp.tsx', terminalSource],
];

const internalProductLanguage = [
  '飞轮', '愿景', '第二套', '内核', '所有权', '权威', '投影', '归属',
  '解耦', '架构', '治理', '事实源', '事实来源', '托管', 'Runtime',
  'STARTING APP PROCESS',
  'ACTIVE PROJECT', 'PAWOS APP', 'ALL APPLICATIONS', 'ROOM DIRECTORY',
  '继续最近的会话', '打开一个 App 开始新的工作',
];

describe('PAWOS product copy boundary', () => {
  it('keeps product vision and implementation rationale out of visible interface copy', () => {
    const violations = visibleSurfaceFiles.flatMap(([relativePath, sourceText]) => {
      const source = ts.createSourceFile(
        relativePath,
        sourceText,
        ts.ScriptTarget.Latest,
        true,
        ts.ScriptKind.TSX,
      );
      return visibleText(source).flatMap((text) => internalProductLanguage
        .filter((phrase) => text.includes(phrase))
        .map((phrase) => `${relativePath}: ${JSON.stringify(text)} includes ${phrase}`));
    });

    expect(violations).toEqual([]);
  });
});

function visibleText(source: ts.SourceFile): string[] {
  const values: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isStringLiteral(node) && isModuleSpecifier(node)) {
      return;
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isJsxText(node)) {
      const value = node.text.trim();
      if (value) values.push(value);
    } else if (ts.isTemplateExpression(node)) {
      const parts = [node.head.text, ...node.templateSpans.map((span) => span.literal.text)];
      values.push(parts.join('').trim());
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return values;
}

function isModuleSpecifier(node: ts.StringLiteral): boolean {
  if ((ts.isImportDeclaration(node.parent) || ts.isExportDeclaration(node.parent))
    && node.parent.moduleSpecifier === node) return true;
  return ts.isCallExpression(node.parent)
    && node.parent.expression.kind === ts.SyntaxKind.ImportKeyword;
}

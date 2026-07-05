import { CodeGraph } from '@colbymchenry/codegraph';
import { ToolHandler, getStaticTools } from '@colbymchenry/codegraph/dist/mcp/tools';

export interface CodeIndex {
  tools: ReturnType<typeof getStaticTools>;
  execute(
    name: string,
    args: Record<string, unknown>,
  ): Promise<{ text: string; isError: boolean }>;
  projectRoot: string;
}

export async function openIndex(projectRoot: string): Promise<CodeIndex> {
  const cg = await CodeGraph.open(projectRoot, { sync: true });
  const handler = new ToolHandler(cg);

  return {
    tools: getStaticTools(),
    projectRoot,
    async execute(name, args) {
      const result = await handler.execute(name, args);
      const text = (result.content ?? [])
        .map((chunk: any) => chunk?.text ?? '')
        .join('\n');
      return { text, isError: !!result.isError };
    },
  };
}

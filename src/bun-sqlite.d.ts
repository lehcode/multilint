// Only the members src/docker-lint.ts uses. @types/bun is not added: its global overlays compete
// with @types/node and the DOM lib.
declare module "bun:sqlite" {
  export class Database {
    constructor(file: string, options?: { readonly?: boolean; create?: boolean });
    exec(sql: string): void;
    query(sql: string): { get(...params: unknown[]): unknown };
    close(): void;
  }
}

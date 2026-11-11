#!/usr/bin/env node

const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require('@modelcontextprotocol/sdk/types.js');

const KHIDR_API = process.env.KHIDR_API_URL || 'http://localhost:8000';
const API_TOKEN = process.env.KHIDR_API_TOKEN;
const PROJECT_ID = process.env.KHIDR_PROJECT_ID;

if (!API_TOKEN || !PROJECT_ID) {
  console.error('KHIDR_API_TOKEN and KHIDR_PROJECT_ID env vars are required');
  process.exit(1);
}

class Mem0MCPServer {
  constructor() {
    this.server = new Server(
      {
        name: 'mem0-mcp',
        version: '1.0.0',
      },
      {
        capabilities: {
          tools: {},
        },
      }
    );

    this.projectId = PROJECT_ID;

    this.server.setRequestHandler(ListToolsRequestSchema, this.handleListTools.bind(this));
    this.server.setRequestHandler(CallToolRequestSchema, this.handleCallTool.bind(this));
  }

  async makeRequest(method, path, body = null) {
    const response = await fetch(`${KHIDR_API}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${API_TOKEN}`
      },
      ...(body ? { body: JSON.stringify(body) } : {})
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API error ${response.status}: ${error}`);
    }

    if (response.status === 204) return null;
    return response.json();
  }

  handleListTools() {
    return {
      tools: [
        {
          name: 'add_memory',
          description: 'Store a piece of information in ProjectMemory for later retrieval',
          inputSchema: {
            type: 'object',
            properties: {
              content: {
                type: 'string',
                description: 'The information to remember'
              },
              importance: {
                type: 'string',
                description: 'Importance level: low, medium, high, critical',
                default: 'medium'
              }
            },
            required: ['content']
          }
        },
        {
          name: 'search_memories',
          description: 'Search through stored ProjectMemories using natural language',
          inputSchema: {
            type: 'object',
            properties: {
              query: {
                type: 'string',
                description: 'Natural language search query'
              },
              limit: {
                type: 'number',
                description: 'Maximum number of results',
                default: 10
              }
            },
            required: ['query']
          }
        },
        {
          name: 'get_memories',
          description: 'List all ProjectMemories for the project',
          inputSchema: {
            type: 'object',
            properties: {
              limit: {
                type: 'number',
                description: 'Maximum number of results',
                default: 50
              }
            }
          }
        },
        {
          name: 'delete_memory',
          description: 'Delete a specific ProjectMemory by ID',
          inputSchema: {
            type: 'object',
            properties: {
              memory_id: {
                type: 'string',
                description: 'ID of the memory to delete'
              }
            },
            required: ['memory_id']
          }
        }
      ]
    };
  }

  async handleCallTool(name, args) {
    try {
      switch (name) {
        case 'add_memory': {
          const result = await this.makeRequest(
            'POST',
            `/api/projects/${this.projectId}/memories/create/`,
            {
              content: args.content,
              importance: args.importance || 'medium'
            }
          );
          return {
            content: [{ type: 'text', text: `Memory stored. ID: ${result.id}` }]
          };
        }

        case 'search_memories': {
          const result = await this.makeRequest(
            'GET',
            `/api/projects/${this.projectId}/memories/search/?q=${encodeURIComponent(args.query)}&limit=${args.limit || 10}`
          );
          return {
            content: [{ type: 'text', text: JSON.stringify(result, null, 2) }]
          };
        }

        case 'get_memories': {
          const result = await this.makeRequest(
            'GET',
            `/api/projects/${this.projectId}/memories/?limit=${args.limit || 50}`
          );
          return {
            content: [{ type: 'text', text: JSON.stringify(result, null, 2) }]
          };
        }

        case 'delete_memory': {
          await this.makeRequest(
            'DELETE',
            `/api/projects/${this.projectId}/memories/${args.memory_id}/`
          );
          return {
            content: [{ type: 'text', text: 'Memory deleted successfully' }]
          };
        }

        default:
          throw new Error(`Unknown tool: ${name}`);
      }
    } catch (error) {
      return {
        content: [{ type: 'text', text: `Error: ${error.message}` }],
        isError: true
      };
    }
  }

  async start() {
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
    console.error('Mem0 MCP Server started with ProjectMemory API');
  }
}

const server = new Mem0MCPServer();
server.start().catch(console.error);
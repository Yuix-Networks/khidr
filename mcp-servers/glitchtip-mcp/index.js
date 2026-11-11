#!/usr/bin/env node
/**
 * GlitchTip Issue MCP Server
 *
 * Provides tools to fetch GlitchTip/Sentry-compatible issue details.
 * Agents can call get_glitchtip_issue with an issue URL to get full details.
 *
 * Usage:
 *   GLITCHTIP_URL=https://glitchtip.example.com \
 *   GLITCHTIP_TOKEN=<api-key> \
 *   node index.js
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

const GLITCHTIP_URL = process.env.GLITCHTIP_URL;
const GLITCHTIP_TOKEN = process.env.GLITCHTIP_TOKEN;

if (!GLITCHTIP_URL || !GLITCHTIP_TOKEN) {
  console.error('GLITCHTIP_URL and GLITCHTIP_TOKEN env vars are required');
  process.exit(1);
}

class GlitchTipMCPServer {
  constructor() {
    this.server = new Server(
      {
        name: 'glitchtip-mcp',
        version: '1.0.0',
      },
      {
        capabilities: {
          tools: {},
        },
      }
    );

    this.server.setRequestHandler(ListToolsRequestSchema, this.handleListTools.bind(this));
    this.server.setRequestHandler(CallToolRequestSchema, this.handleCallTool.bind(this));
  }

  /**
   * Extract issue ID from GlitchTip URL.
   * Handles formats:
   *   - https://glitchtip.example.com/<org>/issues/<id>/
   *   - https://glitchtip.example.com/<org>/issues/<id>
   *   - /<org>/issues/<id>/
   */
  extractIssueId(url) {
    // Match trailing number before optional /
    const match = url.match(/\/issues\/(\d+)\/?$/);
    if (match) {
      return match[1];
    }
    return null;
  }

  async fetchIssue(issueId) {
    const url = `${GLITCHTIP_URL}/api/0/issues/${issueId}/`;
    const response = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${GLITCHTIP_TOKEN}`,
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`GlitchTip API error ${response.status}: ${text}`);
    }

    return response.json();
  }

  handleListTools() {
    return {
      tools: [
        {
          name: 'get_glitchtip_issue',
          description: 'Fetch detailed information about a GlitchTip issue from its URL. Returns title, status,culprit, tag metadata, affected user count, and timestamps.',
          inputSchema: {
            type: 'object',
            properties: {
              issue_url: {
                type: 'string',
                description: 'The full GlitchTip issue URL (e.g. https://glitchtip.example.com/<org>/issues/<id>/)',
              },
              include_events: {
                type: 'boolean',
                description: 'Whether to include recent error events (first 3). Default false.',
                default: false,
              },
            },
            required: ['issue_url'],
          },
        },
      ],
    };
  }

  async handleCallTool(name, args) {
    if (name !== 'get_glitchtip_issue') {
      return {
        content: [{ type: 'text', text: `Unknown tool: ${name}` }],
        isError: true,
      };
    }

    try {
      const issueId = this.extractIssueId(args.issue_url);
      if (!issueId) {
        return {
          content: [{ type: 'text', text: `Could not extract issue ID from URL: ${args.issue_url}. Expected format: https://glitchtip.example.com/<org>/issues/<id>/` }],
          isError: true,
        };
      }

      const issue = await this.fetchIssue(issueId);

      // Format key fields
      const output = [
        `# ${issue.title}`,
        '',
        `**Status:** ${issue.status}`,
        `**Level:** ${issue.level}`,
        `**Culprit:** ${issue.culprit || 'unknown'}`,
        `**Project:** ${issue.project?.name || 'unknown'}`,
        `**Platform:** ${issue.platform || 'unknown'}`,
        '',
        `**First Seen:** ${issue.firstSeen || 'unknown'}`,
        `**Last Seen:** ${issue.lastSeen || 'unknown'}`,
        `**Times Seen:** ${issue.count || 0}`,
        '',
        `**User Count:** ${issue.userCount || 0}`,
        '',
      ];

      // Tags
      if (issue.tags && issue.tags.length > 0) {
        output.push('## Tags');
        for (const tag of issue.tags) {
          output.push(`- ${tag.key}: ${tag.value}`);
        }
        output.push('');
      }

      // Metadata
      if (issue.metadata) {
        output.push('## Metadata');
        if (issue.metadata.title) output.push(`**Title:** ${issue.metadata.title}`);
        if (issue.metadata.type) output.push(`**Type:** ${issue.metadata.type}`);
        if (issue.metadata.culprit) output.push(`**Culprit:** ${issue.metadata.culprit}`);
        output.push('');
      }

      // Short ID
      if (issue.shortId) {
        output.push(`**Short ID:** ${issue.shortId}`);
      }

      // Issue URL
      output.push('');
      output.push(`**URL:** ${args.issue_url}`);

      return {
        content: [{ type: 'text', text: output.join('\n') }],
      };
    } catch (error) {
      return {
        content: [{ type: 'text', text: `Error fetching GlitchTip issue: ${error.message}` }],
        isError: true,
      };
    }
  }

  async start() {
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
    console.error('GlitchTip MCP Server started');
  }
}

const server = new GlitchTipMCPServer();
server.start().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
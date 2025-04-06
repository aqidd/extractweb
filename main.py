from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, LLMConfig
from crawl4ai.extraction_strategy import LLMExtractionStrategy
import os
from dotenv import load_dotenv  # Import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = FastAPI()

from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.browser_manager import BrowserManager

# Patch the AsyncPlaywrightCrawlerStrategy to fix the static instance issue
# This is a workaround for the issue where the static instance of Playwright wasn't being reset
# after closing the browser, causing issues with multiple crawls.
# This patch ensures that the Playwright instance is properly cleaned up after each crawl.
# This is a temporary solution until the issue is resolved in the library.
async def patched_async_playwright__crawler_strategy_close(self) -> None:
    """
    Close the browser and clean up resources.

    This patch addresses an issue with Playwright instance cleanup where the static instance
    wasn't being properly reset, leading to issues with multiple crawls.

    Issue: https://github.com/unclecode/crawl4ai/issues/842

    Returns:
        None
    """
    await self.browser_manager.close()

    # Reset the static Playwright instance
    BrowserManager._playwright_instance = None


AsyncPlaywrightCrawlerStrategy.close = patched_async_playwright__crawler_strategy_close

# Serve static files for the Vue.js frontend
# API routes will be defined before mounting static files

class ExtractionRequest(BaseModel):
    url: str
    instruction: str = None

def generate_schema_from_instruction(instruction: str, llm_provider="openai/gpt-4"):
    """
    Dynamically generates a Pydantic schema using an LLM.
    """
    from pydantic import BaseModel
    from typing import Dict, Any
    # Simple simulated schema inference for demonstration
    class InferredSchema(BaseModel):  # This could be dynamically generated.
        products: list[Dict[str, str]]
    return InferredSchema.schema_json()

class ExtractionRequest(BaseModel):
    url: str
    instruction: str = None  # Optional instruction, defaults to None

class PageSummary(BaseModel):
    title: str = Field(..., description="Title of the page.")
    summary: str = Field(..., description="Summary of the page.")
    keywords: list = Field(..., description="Keywords for the page content.")

@app.post("/extract")
async def extract_data(request: ExtractionRequest):
    try:
        # Configure LLM settings
        llm_config = LLMConfig(
            provider="gemini/gemini-2.0-flash-lite",  # Gemini LLM
            # TODO: provider using env error???
            # provider=os.getenv("LLM_PROVIDER"),  # Load provider from environment variable
            api_token=os.getenv("API_KEY")  # Load API token from environment variable
        )

        # Define the extraction strategy
        if request.instruction and request.instruction.strip():
            # Use schema-based extraction with the provided instruction
            extraction_strategy = LLMExtractionStrategy(
                llm_config=llm_config,
                schema=PageSummary.schema_json(),  # Use the defined schema
                instruction=request.instruction,
                extraction_type="schema",
                chunk_token_threshold=1000,  # Process chunks of 1000 tokens at a time
                verbose=True
            )
        else:
            # Use block-based extraction if no instruction is provided
            extraction_strategy = LLMExtractionStrategy(
                llm_config=llm_config,
                extraction_type="block",  # Uses block-based extraction
                verbose=True
            )

        # Run the web crawler
        async with AsyncWebCrawler() as crawler:
            crawl_config = CrawlerRunConfig(
                extraction_strategy=extraction_strategy,
                cache_mode="BYPASS"  # Bypass cache to ensure fresh content
            )
            result = await crawler.arun(
                url=request.url,
                config=crawl_config
            )

        # Return results if successful
        if result.success:
            return {"schema": result.model_json_schema, "data": result.extracted_content, "raw": result}
        else:
            raise HTTPException(status_code=500, detail=result.error_message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mount static files AFTER defining API routes
app.mount("/static", StaticFiles(directory="static"), name="static")
# Serve index.html at the root
@app.get("/")
async def read_index():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")

import json
import os
import re
from fastapi.responses import FileResponse
import litellm
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, create_model
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, LLMConfig
from crawl4ai.extraction_strategy import LLMExtractionStrategy
from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.browser_manager import BrowserManager
from dotenv import load_dotenv  # Import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = FastAPI()

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

class ExtractionRequest(BaseModel):
    url: str
    instruction: str = None

class PageSummary(BaseModel):
    topic: str = Field(..., description="Title of the topic.")
    keywords: list[str] = Field(..., description="Keywords for the topic content.")
    summary: str = Field(..., description="Summary of the topic.")

# Convert the schema dictionary into a Pydantic model
def schema_dict_to_pydantic(schema_dict):
    fields = {}
    for key, value in schema_dict.get("properties", {}).items():
        field_type = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict
        }.get(value.get("type", "string"), str)
        fields[key] = (field_type, Field(..., description=value.get("description", "")))
    return create_model("DynamicSchema", **fields)

def generate_schema_from_instruction(instruction: str, llm_provider="openai/gpt-4"):
    """
    Dynamically generates a Pydantic schema using an LLM.
    """
    prompt = f"""
    You are an AI that generates JSON schemas based on instructions. 
    Instruction: {instruction}
    Provide a JSON schema that matches the instruction.
    """
    response = litellm.completion(
        model="gemini/gemini-2.0-flash-lite",
        messages=[{"role": "user", "content": prompt}],
        api_key=os.getenv("GEMINI_API_KEY")
    )

    # Extract the response content correctly from the litellm response
    try:
        # The structure from litellm may vary based on provider
        content = response.choices[0].message.content
        
        # Try to extract JSON from markdown code blocks if present
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        
        if json_match:
            # Extract JSON from code block
            schema_dict = json.loads(json_match.group(1))
        else:
            # Try to find any JSON-like structure in the text
            potential_json = re.search(r'(\{[^}]*\})', content, re.DOTALL)
            if potential_json:
                schema_dict = json.loads(potential_json.group(1))
            else:
                # Direct parsing as a last resort
                try:
                    schema_dict = json.loads(content)
                except json.JSONDecodeError:
                    print("Could not parse JSON directly from content")
                    # Create a simple fallback schema if parsing fails
                    schema_dict = {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Extracted content"}
                        }
                    }
        
        if not schema_dict:
            raise ValueError("Empty schema dictionary.")

        return schema_dict_to_pydantic(schema_dict)
    except (KeyError, AttributeError) as e:
        print(f"Error processing LLM response: {e}")
        print(f"Raw response: {response}")
        raise ValueError(f"Failed to generate schema from instruction. Error: {str(e)}")
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        print(f"Raw content: {content}")
        # Return a simple default schema as fallback
        default_schema = PageSummary.model_json_schema()
        return json.dumps(default_schema)

@app.post("/extract")
async def extract_data(request: ExtractionRequest):
    try:
        print(os.getenv("GEMINI_API_KEY"))
        print(os.getenv("GROQ_API_KEY"))
        # Configure LLM settings
        llm_config = LLMConfig(
            provider="gemini/gemini-2.0-flash-lite",  # Gemini LLM
            # api_token=""
            api_token=os.getenv("GEMINI_API_KEY"),  # Load API token from environment variable
            # TODO: provider using env error???
            # provider=os.getenv("LLM_PROVIDER"),  # Load provider from environment variable
            # api_token=os.getenv("API_KEY")  # Load API token f    rom environment variable
        )

        # Define the extraction strategy
        if request.instruction and request.instruction.strip():
            page_schema = generate_schema_from_instruction(request.instruction)
            if not page_schema:
                page_schema = PageSummary.model_json_schema(),  # Use default schema
            # Use schema-based extraction with the provided instruction
            extraction_strategy = LLMExtractionStrategy(
                llm_config=llm_config,
                schema=page_schema,
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
    return FileResponse("static/index.html")

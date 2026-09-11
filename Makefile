# Java agent build -- plain javac + jar, no Maven/Gradle (docs/development.md, "Java toolchain":
# the agent is small and Java is the stated weak spot here, so the build stays as plain as
# possible; reach for a real build tool only if this genuinely stops scaling).
#
# Uses the project-local jdk/ (Azul Zulu) to compile, but --release targets Java 17: confirmed
# via a real local install that Gateway 10.50 bundles Zulu 17 (Zulu17.57+18-SA), not Java 25 --
# that number was from a different install's thread dump and isn't universal. The agent runs
# under whatever JRE Gateway/TWS actually bundles (scripts/launch-agent.sh finds it), so this
# targets the oldest confirmed version, not the newest. Revisit if a newer/older Gateway version
# is confirmed to need something different.

JDK := jdk
JAVAC := $(JDK)/bin/javac --release 17
JAR := $(JDK)/bin/jar
JAVA := $(JDK)/bin/java

SRC := $(wildcard agent/src/main/java/ibcontroller/agent/*.java)

build/ibcontroller-agent.jar: $(SRC)
	mkdir -p build/classes
	$(JAVAC) -d build/classes $(SRC)
	$(JAR) cfe $@ ibcontroller.agent.AgentMain -C build/classes .
	cp build/ibcontroller-agent.jar ibcontroller/

.PHONY: run
run: build/ibcontroller-agent.jar
	$(JAVA) -jar $<

# The Python package can't be built standalone -- `ibcontroller/ibcontroller-agent.jar`
# must exist before `uv build` runs, since it's shipped via [tool.setuptools.package-data]
# the same way ibcontroller/data/*.json is (see pyproject.toml). This target makes that
# real dependency explicit instead of relying on a stale jar left over from `make run`.
.PHONY: dist
dist: build/ibcontroller-agent.jar
	uv build

.PHONY: clean
clean:
	rm -rf build dist
